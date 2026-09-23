"""
backend/droits.py — workflow Pelify Landscape "Demander les droits".

V1 :
- création d'une demande depuis un paysage ;
- conversation persistante ;
- lien invité sécurisé, sans compte photographe ;
- email transactionnel au photographe ;
- réponse du photographe depuis un lien sécurisé ;
- retour de la conversation dans Pelify.

Le lien invité contient un jeton aléatoire. Seul son hash est stocké en base.
Pelify ne transforme pas une réponse du photographe en "droits confirmés" :
la confirmation juridique reste une action explicite de l'utilisateur.
"""

import asyncio
import hashlib
import os
import re
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from pydantic import BaseModel, Field

from db import execute, fetch_all, fetch_one


router = APIRouter(prefix="/api/droits", tags=["Droits Landscape"])

GUEST_TOKEN_SECRET = os.getenv(
    "PELIFY_GUEST_TOKEN_SECRET",
    os.getenv("PELIFY_USER_SECRET", "pelify-dev-secret-change-me"),
)
BASE_URL = os.getenv("PELIFY_BASE_URL", "https://tournage.pelify.app").rstrip("/")
GUEST_TOKEN_DAYS = int(os.getenv("PELIFY_GUEST_TOKEN_DAYS", "14"))

EMAIL_RE = re.compile(r"^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$")

STATUTS_REPONSE = {"message", "acceptee", "refusee", "a_discuter"}


def _hash_guest_token(token: str) -> str:
    return hashlib.sha256(
        f"{GUEST_TOKEN_SECRET}:{token}".encode("utf-8")
    ).hexdigest()


def _email_valide(email: str) -> bool:
    return bool(EMAIL_RE.fullmatch(email.strip()))


def _normaliser_header(value: str) -> str:
    # Évite toute injection d'en-tête SMTP.
    return value.replace("\r", " ").replace("\n", " ").strip()


class DemandeDroitsCreation(BaseModel):
    paysage_id: int
    projet_id: int | None = None
    scene_id: int | None = None

    destinataire_nom: str | None = Field(None, max_length=255)
    destinataire_email: str = Field(..., min_length=3, max_length=320)

    objet: str = Field(..., min_length=3, max_length=500)
    message: str = Field(..., min_length=10, max_length=10000)

    demandeur_email: str | None = Field(None, max_length=320)


class MessageDroitsCreation(BaseModel):
    contenu: str = Field(..., min_length=1, max_length=10000)


class ReponseInvitée(BaseModel):
    contenu: str = Field(..., min_length=1, max_length=10000)
    reponse_type: str = "message"


def _smtp_configure() -> bool:
    return bool(
        os.getenv("SMTP_HOST")
        and os.getenv("SMTP_FROM")
    )


def _envoyer_email_sync(
    destinataire_email: str,
    destinataire_nom: str | None,
    objet: str,
    contenu: str,
    lien: str,
    reply_to: str | None = None,
) -> None:
    host = os.getenv("SMTP_HOST")
    smtp_from = os.getenv("SMTP_FROM")
    if not host or not smtp_from:
        raise RuntimeError(
            "SMTP non configuré : définissez SMTP_HOST et SMTP_FROM."
        )

    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    use_tls = os.getenv("SMTP_USE_TLS", "1") != "0"

    msg = EmailMessage()
    msg["From"] = _normaliser_header(smtp_from)
    msg["To"] = _normaliser_header(destinataire_email)
    msg["Subject"] = _normaliser_header(objet)
    if reply_to:
        msg["Reply-To"] = _normaliser_header(reply_to)

    nom = destinataire_nom or "Bonjour"
    msg.set_content(
        f"""{nom},

Vous avez reçu une demande de droits via Pelify Landscape.

{contenu}

Pour consulter la demande et répondre directement dans Pelify :
{lien}

Ce lien est personnel et valable pendant {GUEST_TOKEN_DAYS} jours.
Vous n'avez pas besoin de créer de compte Pelify.

Pelify Landscape
"""
    )

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
        return

    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.ehlo()
        if use_tls:
            smtp.starttls()
            smtp.ehlo()
        if user and password:
            smtp.login(user, password)
        smtp.send_message(msg)


async def _envoyer_email_demande(demande_id: int, lien: str) -> None:
    """
    Tâche de fond : l'envoi SMTP ne doit pas ralentir la réponse HTTP.
    FastAPI prévoit précisément ce mécanisme pour les notifications email. 
    """
    demande = await fetch_one(
        """SELECT id, destinataire_nom, destinataire_email, objet, message,
                  demandeur_email
           FROM demandes_droits
           WHERE id = %s""",
        (demande_id,),
    )
    if not demande:
        return

    try:
        await asyncio.to_thread(
            _envoyer_email_sync,
            demande["destinataire_email"],
            demande["destinataire_nom"],
            demande["objet"],
            demande["message"],
            lien,
            demande.get("demandeur_email"),
        )
        await execute(
            """UPDATE demandes_droits
               SET email_statut = 'envoye',
                   email_envoye_at = NOW(),
                   email_erreur = NULL
               WHERE id = %s""",
            (demande_id,),
        )
    except Exception as exc:
        await execute(
            """UPDATE demandes_droits
               SET email_statut = 'erreur',
                   email_erreur = %s
               WHERE id = %s""",
            (str(exc)[:2000], demande_id),
        )


async def _notifier_demandeur(demande_id: int, contenu: str, reponse_type: str) -> None:
    demande = await fetch_one(
        """SELECT d.id, d.demandeur_email, d.objet, p.nom AS paysage_nom
           FROM demandes_droits d
           JOIN paysages p ON p.id = d.paysage_id
           WHERE d.id = %s""",
        (demande_id,),
    )
    if not demande or not demande.get("demandeur_email"):
        return

    lien = f"{BASE_URL}/paysages.html"
    sujet = f"Réponse à votre demande de droits — {demande['paysage_nom']}"
    type_label = {
        "acceptee": "Le photographe indique qu'il accepte la demande.",
        "refusee": "Le photographe indique qu'il refuse la demande.",
        "a_discuter": "Le photographe souhaite discuter des conditions.",
        "message": "Le photographe vous a envoyé un message.",
    }.get(reponse_type, "Vous avez reçu une réponse.")

    corps = (
        f"{type_label}\n\n"
        f"{contenu}\n\n"
        f"Consulter la conversation dans Pelify : {lien}"
    )

    try:
        await asyncio.to_thread(
            _envoyer_email_sync,
            demande["demandeur_email"],
            None,
            sujet,
            corps,
            lien,
            None,
        )
    except Exception:
        # Une notification secondaire ne doit pas annuler la réponse.
        pass


async def _profil_depuis_cookie(request: Request, response: Response) -> dict:
    """
    Réutilise l'identité pseudonyme de Landscape Studio.
    Copie volontaire de _ensure_profile pour éviter l'import circulaire.
    """
    import hashlib as _hashlib
    import secrets as _secrets
    import uuid as _uuid

    secret = os.getenv("PELIFY_USER_SECRET", "pelify-dev-secret-change-me")
    cookie_name = "pelify_user"

    token = request.cookies.get(cookie_name)
    created = False
    if not token or len(token) < 32:
        token = _secrets.token_urlsafe(48)
        created = True

    token_hash = _hashlib.sha256(
        f"{secret}:{token}".encode("utf-8")
    ).hexdigest()

    user = await fetch_one(
        "SELECT id FROM pelify_users WHERE visitor_token_hash = %s",
        (token_hash,),
    )

    if not user:
        user_id = str(_uuid.uuid4())
        await execute(
            "INSERT INTO pelify_users (id, visitor_token_hash) VALUES (%s, %s)",
            (user_id, token_hash),
        )
        user = {"id": user_id}
        created = True
    else:
        await execute(
            "UPDATE pelify_users SET last_seen_at = NOW() WHERE id = %s",
            (user["id"],),
        )

    if created:
        response.set_cookie(
            cookie_name,
            token,
            max_age=60 * 60 * 24 * 365,
            httponly=True,
            samesite="lax",
            secure=os.getenv("COOKIE_SECURE", "1") != "0",
            path="/",
        )

    return user


async def _verifier_demandeur(demande_id: int, user_id: str) -> dict:
    demande = await fetch_one(
        """SELECT d.*, p.nom AS paysage_nom, p.image_url, p.thumbnail_url,
                  p.auteur, p.licence, pr.nom AS projet_nom, s.titre AS scene_titre
           FROM demandes_droits d
           JOIN paysages p ON p.id = d.paysage_id
           LEFT JOIN paysage_projets pr ON pr.id = d.projet_id
           LEFT JOIN paysage_scenes s ON s.id = d.scene_id
           WHERE d.id = %s AND d.demandeur_id = %s""",
        (demande_id, user_id),
    )
    if not demande:
        raise HTTPException(404, "Demande introuvable")
    return demande


@router.get("/mes-demandes")
async def mes_demandes(request: Request, response: Response):
    user = await _profil_depuis_cookie(request, response)
    return await fetch_all(
        """SELECT d.id, d.paysage_id, d.projet_id, d.scene_id,
                  d.destinataire_nom, d.destinataire_email,
                  d.objet, d.statut, d.email_statut,
                  d.created_at, d.updated_at,
                  p.nom AS paysage_nom, p.thumbnail_url,
                  pr.nom AS projet_nom, s.titre AS scene_titre
           FROM demandes_droits d
           JOIN paysages p ON p.id = d.paysage_id
           LEFT JOIN paysage_projets pr ON pr.id = d.projet_id
           LEFT JOIN paysage_scenes s ON s.id = d.scene_id
           WHERE d.demandeur_id = %s
           ORDER BY d.updated_at DESC""",
        (user["id"],),
    )


@router.get("/{demande_id}")
async def detail_demande(demande_id: int, request: Request, response: Response):
    user = await _profil_depuis_cookie(request, response)
    demande = await _verifier_demandeur(demande_id, user["id"])
    messages = await fetch_all(
        """SELECT id, expediteur_type, expediteur_nom, contenu,
                  reponse_type, lu, created_at
           FROM messages_droits
           WHERE demande_id = %s
           ORDER BY created_at""",
        (demande_id,),
    )
    return {"demande": demande, "messages": messages}


@router.post("", status_code=201)
async def creer_demande(
    payload: DemandeDroitsCreation,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
):
    user = await _profil_depuis_cookie(request, response)

    email = payload.destinataire_email.strip().lower()
    if not _email_valide(email):
        raise HTTPException(400, "Adresse email du photographe invalide.")

    if payload.demandeur_email:
        demandeur_email = payload.demandeur_email.strip().lower()
        if not _email_valide(demandeur_email):
            raise HTTPException(400, "Votre adresse email est invalide.")
    else:
        demandeur_email = None

    paysage = await fetch_one(
        """SELECT id, nom, auteur, auteur_url, source_url
           FROM paysages
           WHERE id = %s""",
        (payload.paysage_id,),
    )
    if not paysage:
        raise HTTPException(404, "Paysage introuvable")

    if payload.projet_id is not None:
        projet = await fetch_one(
            "SELECT id FROM paysage_projets WHERE id = %s AND user_id = %s",
            (payload.projet_id, user["id"]),
        )
        if not projet:
            raise HTTPException(404, "Projet introuvable")

    if payload.scene_id is not None:
        scene = await fetch_one(
            """SELECT s.id
               FROM paysage_scenes s
               JOIN paysage_projets p ON p.id = s.projet_id
               WHERE s.id = %s AND p.user_id = %s""",
            (payload.scene_id, user["id"]),
        )
        if not scene:
            raise HTTPException(404, "Scène introuvable")

    token = secrets.token_urlsafe(48)
    token_hash = _hash_guest_token(token)
    expiration = datetime.now(timezone.utc) + timedelta(days=GUEST_TOKEN_DAYS)
    lien = f"{BASE_URL}/droits/{token}"

    email_statut = "a_envoyer" if _smtp_configure() else "non_configure"

    # email_lien_temporaire est supprimé après l'envoi.
    demande_id = await execute(
        """INSERT INTO demandes_droits (
               paysage_id, projet_id, scene_id, demandeur_id,
               destinataire_nom, destinataire_email,
               objet, message, statut,
               guest_token_hash, guest_token_expires_at,
               email_statut, demandeur_email
           )
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'en_attente',%s,%s,%s,%s)
           RETURNING id""",
        (
            payload.paysage_id,
            payload.projet_id,
            payload.scene_id,
            user["id"],
            payload.destinataire_nom.strip() if payload.destinataire_nom else None,
            email,
            payload.objet.strip(),
            payload.message.strip(),
            token_hash,
            expiration,
            email_statut,
            demandeur_email,
        ),
    )

    await execute(
        """INSERT INTO messages_droits
           (demande_id, expediteur_type, expediteur_id, contenu, reponse_type)
           VALUES (%s,'pelify',%s,%s,'message')""",
        (demande_id, user["id"], payload.message.strip()),
    )

    if _smtp_configure():
        background_tasks.add_task(_envoyer_email_demande, demande_id, lien)

    return {
        "id": demande_id,
        "statut": "en_attente",
        "email_statut": email_statut,
        "guest_link": lien,
        "message": (
            "Demande enregistrée. L'email sera envoyé automatiquement."
            if _smtp_configure()
            else
            "Demande enregistrée. Configurez SMTP pour activer l'envoi automatique."
        ),
    }


@router.post("/{demande_id}/messages", status_code=201)
async def ajouter_message_demande(
    demande_id: int,
    payload: MessageDroitsCreation,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
):
    user = await _profil_depuis_cookie(request, response)
    demande = await _verifier_demandeur(demande_id, user["id"])

    message_id = await execute(
        """INSERT INTO messages_droits
           (demande_id, expediteur_type, expediteur_id, contenu, reponse_type)
           VALUES (%s,'pelify',%s,%s,'message')
           RETURNING id""",
        (demande_id, user["id"], payload.contenu.strip()),
    )

    # Une vraie relance par email du photographe sera ajoutée au V2.
    return {"id": message_id}


async def _demande_depuis_token(token: str) -> dict:
    if not token or len(token) < 40:
        raise HTTPException(404, "Lien invalide ou expiré.")

    token_hash = _hash_guest_token(token)
    demande = await fetch_one(
        """SELECT d.id, d.paysage_id, d.destinataire_nom, d.destinataire_email,
                  d.objet, d.message, d.statut, d.guest_token_expires_at,
                  p.nom AS paysage_nom, p.image_url, p.thumbnail_url,
                  p.auteur, p.source_url
           FROM demandes_droits d
           JOIN paysages p ON p.id = d.paysage_id
           WHERE d.guest_token_hash = %s""",
        (token_hash,),
    )
    if not demande:
        raise HTTPException(404, "Lien invalide ou expiré.")

    expiration = demande["guest_token_expires_at"]
    if expiration and expiration < datetime.now(timezone.utc):
        await execute(
            "UPDATE demandes_droits SET statut = 'expiree' WHERE id = %s AND statut = 'en_attente'",
            (demande["id"],),
        )
        raise HTTPException(410, "Ce lien de réponse a expiré.")

    return demande


@router.get("/public/{token}")
async def demande_publique(token: str):
    demande = await _demande_depuis_token(token)
    messages = await fetch_all(
        """SELECT id, expediteur_type, expediteur_nom, contenu,
                  reponse_type, created_at
           FROM messages_droits
           WHERE demande_id = %s
           ORDER BY created_at""",
        (demande["id"],),
    )
    return {"demande": demande, "messages": messages}


@router.post("/public/{token}/messages", status_code=201)
async def repondre_demande_publique(
    token: str,
    payload: ReponseInvitée,
    background_tasks: BackgroundTasks,
):
    demande = await _demande_depuis_token(token)

    if payload.reponse_type not in STATUTS_REPONSE:
        raise HTTPException(
            400,
            f"reponse_type invalide, attendu parmi {sorted(STATUTS_REPONSE)}",
        )

    contenu = payload.contenu.strip()
    statut = {
        "acceptee": "acceptee",
        "refusee": "refusee",
        "a_discuter": "a_discuter",
        "message": "reponse_recue",
    }[payload.reponse_type]

    message_id = await execute(
        """INSERT INTO messages_droits
           (demande_id, expediteur_type, expediteur_nom, expediteur_email,
            contenu, reponse_type)
           VALUES (%s,'photographe',%s,%s,%s,%s)
           RETURNING id""",
        (
            demande["id"],
            demande.get("destinataire_nom"),
            demande.get("destinataire_email"),
            contenu,
            payload.reponse_type,
        ),
    )

    await execute(
        "UPDATE demandes_droits SET statut = %s WHERE id = %s",
        (statut, demande["id"]),
    )

    background_tasks.add_task(
        _notifier_demandeur,
        demande["id"],
        contenu,
        payload.reponse_type,
    )

    return {"id": message_id, "statut": statut}
