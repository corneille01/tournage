"""
backend/communaute.py

Communauté « après la visite » : partager photos et récits d'un lieu, commenter en temps réel.

Principes (détails dans docs/ARCHITECTURE_PELIFY.md) :
- PSEUDONYMAT : aucun nom, e-mail ni position exigés. L'identité réutilise le cookie pseudonyme existant
  de Pelify (pelify_users) ; un profil communautaire = un pseudo + l'acceptation des règles.
- MINIMISATION & DROITS RGPD : export de ses données (GET /mes-donnees), effacement complet (DELETE /mon-compte,
  y compris les fichiers du stockage), aucun suivi publicitaire.
- MODÉRATION PROPORTIONNÉE : publication immédiate du texte ; mise en attente si lien, coordonnées personnelles,
  mots interdits, ou premières photos d'un nouveau membre ; signalements (DSA « notice-and-action ») avec
  masquage automatique au 3ᵉ signalement distinct, immédiat pour « mineur » ; exposé des motifs journalisé.
- ANTI-ABUS : limites de débit par membre (posts, commentaires, signalements, photos).
- TEMPS RÉEL : flux SSE par publication (commentaires et compteurs), sans WebSocket ni service tiers.
- PHOTOS UNIQUEMENT pour l'instant (vidéo : phase 2 avec Cloudflare Stream, modération dédiée).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import unicodedata
import uuid
from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

import stockage_media
from db import execute, fetch_all, fetch_one

logger = logging.getLogger(__name__)
router = APIRouter()

REGLES_VERSION = "1.0"
COOKIE = "pelify_user"
SECRET = os.getenv("PELIFY_USER_SECRET", "pelify-dev-secret-change-me")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
MOTS_INTERDITS = [m.strip().lower() for m in os.getenv("COMMUNAUTE_MOTS_INTERDITS", "").split(",") if m.strip()]

MAX_PHOTOS_PAR_POST = 4
MAX_OCTETS_PHOTO = 6 * 1024 * 1024
MIMES_PHOTO = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
SEUIL_CONFIANCE = 3                 # posts validés avant de pouvoir publier photos/liens sans attente
SEUIL_MASQUAGE_AUTO = 3             # signalements distincts avant masquage automatique
MOTIFS = {"illegal", "haine", "harcelement", "vie_privee", "spam", "mineur", "droit_auteur", "autre"}
PSEUDOS_RESERVES = ("admin", "moderat", "pelify", "equipe", "support", "officiel", "staff")
RE_LIEN = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|fr|net|org|io|app|info|ru|xyz)\b)", re.I)
RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
RE_TEL = re.compile(r"(?<!\d)(?:\+?\d[\s.-]?){9,}(?!\d)")
RE_PSEUDO = re.compile(r"^[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9 _.-]{2,23}$")
_FLUX_ACTIFS = 0
FLUX_MAX = 300

REGLES = {
    "version": REGLES_VERSION,
    "titre": "Règles de la communauté Pelify",
    "regles": [
        "Partagez ce que vous avez vécu sur place : photos que vous avez prises, impressions, conseils pratiques.",
        "Respectez les autres : pas d'insultes, de harcèlement, de discrimination ni d'appel à la haine.",
        "Respectez la vie privée : ne publiez pas de visage reconnaissable d'une personne sans son accord, ni de plaque d'immatriculation, d'adresse, de téléphone ou d'e-mail. Les enfants ne doivent pas être identifiables.",
        "Ne publiez que des photos dont vous êtes l'auteur. Pas de captures d'écran de films ou de séries (droit d'auteur).",
        "Respectez les lieux : n'encouragez pas l'intrusion dans une propriété privée, les zones interdites ou dangereuses.",
        "Pas de publicité, de liens commerciaux ni de contenus illégaux. Les nouveaux membres ne peuvent pas publier de liens.",
    ],
    "donnees": [
        "Aucune adresse e-mail, aucun nom n'est demandé : vous êtes identifié par un pseudonyme et un cookie technique (pas de publicité, pas de suivi).",
        "Vos publications sont publiques. Les métadonnées de position (EXIF) de vos photos sont supprimées avant l'envoi.",
        "Vous pouvez à tout moment télécharger vos données et supprimer votre profil et tous vos contenus (page Communauté › Mes données).",
        "Les contenus supprimés par leur auteur sont effacés. Les contenus retirés par la modération sont conservés 6 mois à titre de preuve, puis effacés.",
    ],
    "moderation": [
        "Tout contenu peut être signalé (bouton « Signaler »). Le contenu est examiné ; un signalement pour mineur en danger ou contenu illégal le masque immédiatement.",
        "En cas de retrait ou de suspension, vous êtes informé du motif et pouvez contester en écrivant à l'équipe.",
    ],
    "contact": "reservations@pelify.app",
}


# ───────────── identité pseudonyme ─────────────

def _hash_token(token: str) -> str:
    return hashlib.sha256((SECRET + ":" + token).encode("utf-8")).hexdigest()


async def _utilisateur(request: Request, response: Response) -> dict:
    """Même mécanisme que _ensure_profile (main.py) : cookie aléatoire, seul son hash est stocké."""
    token = request.cookies.get(COOKIE)
    cree = False
    if not token or len(token) < 32:
        token, cree = secrets.token_urlsafe(48), True
    h = _hash_token(token)
    user = await fetch_one("SELECT id FROM pelify_users WHERE visitor_token_hash = %s", (h,))
    if not user:
        uid = str(uuid.uuid4())
        await execute("INSERT INTO pelify_users (id, visitor_token_hash) VALUES (%s, %s)", (uid, h))
        user, cree = {"id": uid}, True
    if cree:
        response.set_cookie(COOKIE, token, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax",
                            secure=os.getenv("COOKIE_SECURE", "1") != "0", path="/")
    return {"id": str(user["id"])}


async def _profil_requis(request: Request, response: Response) -> dict:
    u = await _utilisateur(request, response)
    p = await fetch_one("SELECT user_id, pseudo, statut, posts_valides FROM communaute_profils WHERE user_id = %s", (u["id"],))
    if not p:
        raise HTTPException(403, "Créez d'abord votre profil de communauté (pseudo + acceptation des règles).")
    if p["statut"] != "actif":
        raise HTTPException(403, "Votre profil est suspendu ou banni. Contactez l'équipe pour contester la décision.")
    return {**dict(p), "user_id": str(p["user_id"])}


def exiger_admin(request: Request) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(503, "Administration non configurée (variable ADMIN_TOKEN manquante)")
    fourni = request.headers.get("X-Admin-Token", "")
    if not hmac.compare_digest(fourni.encode(), ADMIN_TOKEN.encode()):
        raise HTTPException(401, "Jeton d'administration invalide")


def _norm(s: str) -> str:
    t = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", t)


def _nettoyer(texte, maxi: int) -> str:
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\u200b-\u200f\u202a-\u202e]", "", str(texte or ""))
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t[:maxi]


async def _limite(sql: str, params: tuple, maxi: int, message: str) -> None:
    ligne = await fetch_one(sql, params)
    if ligne and int(ligne["n"]) >= maxi:
        raise HTTPException(429, message)


def _raisons_attente(texte: str, nb_medias: int, profil: dict) -> list[str]:
    raisons = []
    nouveau = int(profil.get("posts_valides") or 0) < SEUIL_CONFIANCE
    if RE_LIEN.search(texte):
        raisons.append("lien externe")
    if RE_EMAIL.search(texte) or RE_TEL.search(texte):
        raisons.append("coordonnées personnelles possibles")
    low = texte.lower()
    if any(m in low for m in MOTS_INTERDITS):
        raisons.append("terme à vérifier")
    if re.search(r"(.)\1{9,}", texte):
        raisons.append("répétitions suspectes")
    if nouveau and nb_medias:
        raisons.append("premières photos d'un nouveau membre")
    return raisons


# ───────────── recherche de lieu (formulaire de partage) ─────────────

_LIEUX: dict = {"t": 0.0, "data": None}


@router.get("/api/communaute/lieux")
async def chercher_lieux(q: str = Query("", max_length=60), limite: int = Query(15, ge=1, le=30)):
    """Recherche rapide (sans accents) d'un lieu de tournage par nom, commune ou titre de l'œuvre."""
    import time
    if _LIEUX["data"] is None or time.monotonic() - _LIEUX["t"] > 600:
        rows = await fetch_all("SELECT l.id, l.nom, l.commune, f.id AS film_id, f.titre FROM lieux_tournage l "
                               "JOIN films f ON f.id = l.film_id AND f.statut = 'publie' ORDER BY f.titre, l.nom")
        _LIEUX.update(t=time.monotonic(), data=[{**dict(r), "_n": re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", f"{r['nom']} {r['commune']} {r['titre']}").encode("ascii", "ignore").decode().lower())} for r in rows])
    mots = [m for m in re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", q).encode("ascii", "ignore").decode().lower()).split() if m]
    trouves = [x for x in _LIEUX["data"] if all(m in x["_n"] for m in mots)] if mots else _LIEUX["data"][:limite]
    return {"lieux": [{"id": x["id"], "nom": x["nom"], "commune": x["commune"], "film_id": x["film_id"], "film_titre": x["titre"]} for x in trouves[:limite]]}


# ───────────── règles et profil ─────────────

@router.get("/api/communaute/regles")
async def regles():
    return REGLES


@router.get("/api/communaute/moi")
async def moi(request: Request, response: Response):
    u = await _utilisateur(request, response)
    p = await fetch_one("SELECT pseudo, statut, posts_valides, regles_version FROM communaute_profils WHERE user_id = %s", (u["id"],))
    return {"profil": dict(p) if p else None, "regles_version": REGLES_VERSION, "photos_possibles": stockage_media.configure()}


@router.post("/api/communaute/profil")
async def creer_profil(request: Request, response: Response):
    u = await _utilisateur(request, response)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON invalide")
    if body.get("accepte_regles") is not True:
        raise HTTPException(400, "Vous devez accepter les règles de la communauté.")
    pseudo = re.sub(r"\s+", " ", str(body.get("pseudo") or "")).strip()
    if not RE_PSEUDO.match(pseudo):
        raise HTTPException(400, "Pseudo invalide : 3 à 24 caractères (lettres, chiffres, espace, _ . -).")
    norm = _norm(pseudo)
    if any(r in norm for r in PSEUDOS_RESERVES) or RE_EMAIL.search(pseudo) or RE_LIEN.search(pseudo):
        raise HTTPException(400, "Ce pseudo n'est pas autorisé (réservé, adresse ou lien).")
    if await fetch_one("SELECT 1 AS x FROM communaute_profils WHERE user_id = %s", (u["id"],)):
        raise HTTPException(409, "Vous avez déjà un profil.")
    if await fetch_one("SELECT 1 AS x FROM communaute_profils WHERE pseudo_norm = %s", (norm,)):
        raise HTTPException(409, "Ce pseudo est déjà pris.")
    await execute("INSERT INTO communaute_profils (user_id, pseudo, pseudo_norm, regles_version) VALUES (%s, %s, %s, %s)",
                  (u["id"], pseudo, norm, REGLES_VERSION))
    return {"pseudo": pseudo, "regles_version": REGLES_VERSION}


# ───────────── photos ─────────────

@router.post("/api/communaute/medias/intention")
async def intention_media(request: Request, response: Response):
    """Étape 1 : le navigateur demande une URL pré-signée, envoie sa photo directement au stockage,
    puis référence `media_id` dans la création du post."""
    if not stockage_media.configure():
        raise HTTPException(503, "L'envoi de photos n'est pas encore activé : publiez en texte pour l'instant.")
    p = await _profil_requis(request, response)
    body = await request.json()
    mime = str(body.get("mime") or "")
    taille = int(body.get("taille") or 0)
    if mime not in MIMES_PHOTO:
        raise HTTPException(400, "Format non accepté (JPEG, PNG ou WebP).")
    if taille <= 0 or taille > MAX_OCTETS_PHOTO:
        raise HTTPException(400, f"Photo trop lourde (maximum {MAX_OCTETS_PHOTO // (1024 * 1024)} Mo).")
    await _limite("SELECT count(*) AS n FROM communaute_medias WHERE auteur_id = %s AND cree_le > now() - interval '1 hour'",
                  (p["user_id"],), 12, "Trop de photos envoyées récemment. Réessayez plus tard.")
    cle = f"communaute/{datetime.utcnow():%Y/%m}/{uuid.uuid4().hex}.{MIMES_PHOTO[mime]}"
    media_id = await execute(
        "INSERT INTO communaute_medias (auteur_id, cle_stockage, type, mime, taille_octets, largeur, hauteur, alt_texte) "
        "VALUES (%s, %s, 'photo', %s, %s, %s, %s, %s) RETURNING id",
        (p["user_id"], cle, mime, taille, body.get("largeur"), body.get("hauteur"), _nettoyer(body.get("alt"), 200) or None))
    return {"media_id": media_id, "url_televersement": stockage_media.url_presignee("PUT", cle, 600, mime),
            "en_tetes": {"Content-Type": mime}, "expire_dans_s": 600}


# ───────────── publications ─────────────

def _publication(r: dict, medias: list, moi_id: str | None, likes_moi: set) -> dict:
    return {
        "id": r["id"], "titre": r["titre"], "texte": r["texte"], "note": r["note"], "date_visite": r["date_visite"].isoformat() if r["date_visite"] else None,
        "statut": r["statut"], "cree_le": r["cree_le"].isoformat(), "nb_likes": r["nb_likes"], "nb_commentaires": r["nb_commentaires"],
        "auteur": {"pseudo": r["pseudo"]}, "a_moi": moi_id is not None and str(r["auteur_id"]) == moi_id,
        "like_par_moi": r["id"] in likes_moi,
        "lieu": {"id": r["lieu_id"], "nom": r["lieu_nom"], "commune": r["commune"]} if r["lieu_id"] else None,
        "film": {"id": r["film_id"], "titre": r["film_titre"]} if r["film_id"] else None,
        "medias": [{"id": m["id"], "url": stockage_media.url_publique(m["cle_stockage"]) if stockage_media.configure() else None,
                    "largeur": m["largeur"], "hauteur": m["hauteur"], "alt": m["alt_texte"]} for m in medias],
    }


async def _charger(posts: list, moi_id: str | None) -> list[dict]:
    if not posts:
        return []
    ids = [p["id"] for p in posts]
    medias = await fetch_all("SELECT id, post_id, cle_stockage, largeur, hauteur, alt_texte FROM communaute_medias "
                             "WHERE post_id = ANY(%s) AND statut = 'valide' ORDER BY id", (ids,))
    par_post: dict[int, list] = {}
    for m in medias:
        par_post.setdefault(m["post_id"], []).append(m)
    likes = set()
    if moi_id:
        likes = {r["post_id"] for r in await fetch_all("SELECT post_id FROM communaute_reactions WHERE user_id = %s AND post_id = ANY(%s)", (moi_id, ids))}
    return [_publication(p, par_post.get(p["id"], []), moi_id, likes) for p in posts]


_SELECT_POST = """
    SELECT p.id, p.auteur_id, p.titre, p.texte, p.note, p.date_visite, p.statut, p.cree_le, p.nb_likes, p.nb_commentaires,
           p.lieu_id, p.film_id, pr.pseudo, l.nom AS lieu_nom, l.commune, f.titre AS film_titre
    FROM communaute_posts p JOIN communaute_profils pr ON pr.user_id = p.auteur_id
    LEFT JOIN lieux_tournage l ON l.id = p.lieu_id LEFT JOIN films f ON f.id = p.film_id
"""


@router.get("/api/communaute/posts")
async def liste_posts(request: Request, response: Response, lieu_id: int | None = Query(None), film_id: int | None = Query(None),
                      tri: str = Query("recent"), avant_id: int | None = Query(None), limite: int = Query(15, ge=1, le=30)):
    u = await _utilisateur(request, response)
    cond, params = ["(p.statut = 'publie' OR (p.statut = 'en_attente' AND p.auteur_id = %s))"], [u["id"]]
    if lieu_id is not None:
        cond.append("p.lieu_id = %s"); params.append(lieu_id)
    if film_id is not None:
        cond.append("(p.film_id = %s)"); params.append(film_id)
    if avant_id is not None and tri == "recent":
        cond.append("p.id < %s"); params.append(avant_id)
    ordre = "p.id DESC" if tri == "recent" else "(p.nb_likes * 2 + p.nb_commentaires) DESC, p.id DESC"
    if tri != "recent":
        cond.append("p.cree_le > now() - interval '90 days'")
    rows = await fetch_all(f"{_SELECT_POST} WHERE {' AND '.join(cond)} ORDER BY {ordre} LIMIT %s", tuple(params + [limite]))
    posts = await _charger([dict(r) for r in rows], u["id"])
    return {"posts": posts, "suivant": posts[-1]["id"] if len(posts) == limite and tri == "recent" else None}


@router.post("/api/communaute/posts")
async def creer_post(request: Request, response: Response):
    p = await _profil_requis(request, response)
    body = await request.json()
    texte = _nettoyer(body.get("texte"), 4000)
    if len(texte) < 10:
        raise HTTPException(400, "Votre récit est trop court (10 caractères minimum).")
    titre = _nettoyer(body.get("titre"), 120) or None
    lieu_id, film_id = body.get("lieu_id"), body.get("film_id")
    if lieu_id is None and film_id is None:
        raise HTTPException(400, "Indiquez le lieu de tournage ou l'œuvre concernée.")
    if lieu_id is not None:
        lieu = await fetch_one("SELECT id, film_id FROM lieux_tournage WHERE id = %s", (int(lieu_id),))
        if not lieu:
            raise HTTPException(404, "Lieu introuvable")
        film_id = film_id or lieu["film_id"]
    if film_id is not None and not await fetch_one("SELECT 1 AS x FROM films WHERE id = %s AND statut = 'publie'", (int(film_id),)):
        raise HTTPException(404, "Œuvre introuvable")
    note = body.get("note")
    note = int(note) if note not in (None, "") and 1 <= int(note) <= 5 else None
    try:
        date_visite = date.fromisoformat(str(body.get("date_visite"))) if body.get("date_visite") else None
        if date_visite and date_visite > date.today():
            raise ValueError
    except ValueError:
        raise HTTPException(400, "Date de visite invalide.")
    media_ids = [int(x) for x in (body.get("media_ids") or [])][:MAX_PHOTOS_PAR_POST]

    await _limite("SELECT count(*) AS n FROM communaute_posts WHERE auteur_id = %s AND cree_le > now() - interval '1 hour'", (p["user_id"],), 5,
                  "Vous publiez très vite : patientez un peu avant de poster à nouveau.")
    await _limite("SELECT count(*) AS n FROM communaute_posts WHERE auteur_id = %s AND cree_le > now() - interval '1 day'", (p["user_id"],), 20,
                  "Limite quotidienne de publications atteinte.")

    medias = []
    if media_ids:
        medias = await fetch_all("SELECT id, cle_stockage, mime, taille_octets FROM communaute_medias WHERE id = ANY(%s) AND auteur_id = %s AND post_id IS NULL AND statut = 'televerse'",
                                 (media_ids, p["user_id"]))
        if len(medias) != len(set(media_ids)):
            raise HTTPException(400, "Une photo est introuvable ou déjà utilisée.")
        for m in medias:   # le fichier doit réellement exister, avoir le bon type et une taille raisonnable
            infos = await stockage_media.verifier_objet(m["cle_stockage"])
            if not infos or infos["taille"] > MAX_OCTETS_PHOTO or not infos["mime"].startswith(m["mime"]):
                raise HTTPException(400, "L'envoi d'une photo n'a pas abouti ou n'est pas conforme. Réessayez.")

    raisons = _raisons_attente(f"{titre or ''}\n{texte}", len(medias), p)
    statut = "en_attente" if raisons else "publie"
    post_id = await execute(
        "INSERT INTO communaute_posts (auteur_id, lieu_id, film_id, titre, texte, note, date_visite, statut, motif_moderation) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (p["user_id"], int(lieu_id) if lieu_id is not None else None, int(film_id) if film_id is not None else None, titre, texte, note,
         date_visite, statut, ", ".join(raisons) or None))
    for m in medias:
        await execute("UPDATE communaute_medias SET post_id = %s, statut = 'valide' WHERE id = %s", (post_id, m["id"]))
    if statut == "publie":
        await execute("UPDATE communaute_profils SET posts_valides = posts_valides + 1 WHERE user_id = %s", (p["user_id"],))
    return {"id": post_id, "statut": statut,
            "message": "Publié !" if statut == "publie" else f"Votre publication est en attente de relecture ({', '.join(raisons)}). Elle apparaîtra dès validation."}


async def _supprimer_medias_post(post_ids: list[int]) -> None:
    rows = await fetch_all("SELECT id, cle_stockage FROM communaute_medias WHERE post_id = ANY(%s)", (post_ids,))
    if stockage_media.configure():
        await asyncio.gather(*(stockage_media.supprimer_objet(r["cle_stockage"]) for r in rows), return_exceptions=True)


@router.delete("/api/communaute/posts/{post_id}")
async def supprimer_post(post_id: int, request: Request, response: Response):
    u = await _utilisateur(request, response)
    r = await fetch_one("SELECT id FROM communaute_posts WHERE id = %s AND auteur_id = %s", (post_id, u["id"]))
    if not r:
        raise HTTPException(404, "Publication introuvable")
    await _supprimer_medias_post([post_id])
    await execute("DELETE FROM communaute_posts WHERE id = %s", (post_id,))     # suppression réelle : droit à l'effacement
    return {"supprime": True}


@router.post("/api/communaute/posts/{post_id}/like")
async def like(post_id: int, request: Request, response: Response):
    p = await _profil_requis(request, response)
    if not await fetch_one("SELECT 1 AS x FROM communaute_posts WHERE id = %s AND statut = 'publie'", (post_id,)):
        raise HTTPException(404, "Publication introuvable")
    deja = await fetch_one("SELECT 1 AS x FROM communaute_reactions WHERE post_id = %s AND user_id = %s", (post_id, p["user_id"]))
    if deja:
        await execute("DELETE FROM communaute_reactions WHERE post_id = %s AND user_id = %s", (post_id, p["user_id"]))
        await execute("UPDATE communaute_posts SET nb_likes = GREATEST(0, nb_likes - 1) WHERE id = %s", (post_id,))
    else:
        await execute("INSERT INTO communaute_reactions (post_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (post_id, p["user_id"]))
        await execute("UPDATE communaute_posts SET nb_likes = nb_likes + 1 WHERE id = %s", (post_id,))
    r = await fetch_one("SELECT nb_likes FROM communaute_posts WHERE id = %s", (post_id,))
    return {"like": not deja, "nb_likes": r["nb_likes"]}


# ───────────── commentaires (temps réel) ─────────────

def _commentaire(c) -> dict:
    return {"id": c["id"], "post_id": c["post_id"], "parent_id": c["parent_id"], "texte": c["texte"],
            "cree_le": c["cree_le"].isoformat(), "auteur": {"pseudo": c["pseudo"]}}


_SELECT_COM = ("SELECT c.id, c.post_id, c.parent_id, c.texte, c.cree_le, pr.pseudo FROM communaute_commentaires c "
               "JOIN communaute_profils pr ON pr.user_id = c.auteur_id")


@router.get("/api/communaute/posts/{post_id}/commentaires")
async def liste_commentaires(post_id: int, apres_id: int = Query(0, ge=0)):
    rows = await fetch_all(f"{_SELECT_COM} WHERE c.post_id = %s AND c.statut = 'publie' AND c.id > %s ORDER BY c.id LIMIT 200", (post_id, apres_id))
    return {"commentaires": [_commentaire(r) for r in rows]}


@router.post("/api/communaute/posts/{post_id}/commentaires")
async def commenter(post_id: int, request: Request, response: Response):
    p = await _profil_requis(request, response)
    body = await request.json()
    texte = _nettoyer(body.get("texte"), 1500)
    if len(texte) < 1:
        raise HTTPException(400, "Commentaire vide.")
    if not await fetch_one("SELECT 1 AS x FROM communaute_posts WHERE id = %s AND statut = 'publie'", (post_id,)):
        raise HTTPException(404, "Publication introuvable")
    nouveau = int(p.get("posts_valides") or 0) < SEUIL_CONFIANCE
    if (nouveau and RE_LIEN.search(texte)) or RE_EMAIL.search(texte) or RE_TEL.search(texte) or any(m in texte.lower() for m in MOTS_INTERDITS):
        raise HTTPException(400, "Ce commentaire ne peut pas être publié (lien, coordonnées personnelles ou terme non autorisé).")
    await _limite("SELECT count(*) AS n FROM communaute_commentaires WHERE auteur_id = %s AND cree_le > now() - interval '10 minutes'",
                  (p["user_id"],), 20, "Vous commentez très vite : patientez quelques minutes.")
    parent = body.get("parent_id")
    if parent is not None and not await fetch_one("SELECT 1 AS x FROM communaute_commentaires WHERE id = %s AND post_id = %s", (int(parent), post_id)):
        raise HTTPException(404, "Commentaire parent introuvable")
    cid = await execute("INSERT INTO communaute_commentaires (post_id, parent_id, auteur_id, texte) VALUES (%s, %s, %s, %s) RETURNING id",
                        (post_id, int(parent) if parent is not None else None, p["user_id"], texte))
    await execute("UPDATE communaute_posts SET nb_commentaires = nb_commentaires + 1 WHERE id = %s", (post_id,))
    c = await fetch_one(f"{_SELECT_COM} WHERE c.id = %s", (cid,))
    return _commentaire(c)


@router.delete("/api/communaute/commentaires/{commentaire_id}")
async def supprimer_commentaire(commentaire_id: int, request: Request, response: Response):
    u = await _utilisateur(request, response)
    c = await fetch_one("SELECT id, post_id FROM communaute_commentaires WHERE id = %s AND auteur_id = %s", (commentaire_id, u["id"]))
    if not c:
        raise HTTPException(404, "Commentaire introuvable")
    await execute("DELETE FROM communaute_commentaires WHERE id = %s", (commentaire_id,))
    await execute("UPDATE communaute_posts SET nb_commentaires = GREATEST(0, nb_commentaires - 1) WHERE id = %s", (c["post_id"],))
    return {"supprime": True}


@router.get("/api/communaute/posts/{post_id}/flux")
async def flux(post_id: int, request: Request, apres_id: int = Query(0, ge=0)):
    """Server-Sent Events : nouveaux commentaires et compteurs. Interrogation de la base toutes les 3 s
    (fonctionne avec plusieurs instances du serveur, sans Redis ni WebSocket)."""
    if _FLUX_ACTIFS >= FLUX_MAX:
        raise HTTPException(503, "Trop de connexions en direct, réessayez dans un instant.")

    async def generateur():
        global _FLUX_ACTIFS
        _FLUX_ACTIFS += 1
        dernier, ecoule = apres_id, 0
        try:
            yield "retry: 3000\n\n"
            while ecoule < 900 and not await request.is_disconnected():
                rows = await fetch_all(f"{_SELECT_COM} WHERE c.post_id = %s AND c.statut = 'publie' AND c.id > %s ORDER BY c.id LIMIT 50", (post_id, dernier))
                for r in rows:
                    dernier = r["id"]
                    yield f"event: commentaire\ndata: {json.dumps(_commentaire(r), ensure_ascii=False)}\n\n"
                cpt = await fetch_one("SELECT nb_likes, nb_commentaires, statut FROM communaute_posts WHERE id = %s", (post_id,))
                if not cpt or cpt["statut"] != "publie":
                    yield "event: fin\ndata: {}\n\n"
                    return
                yield f"event: compteurs\ndata: {json.dumps({'nb_likes': cpt['nb_likes'], 'nb_commentaires': cpt['nb_commentaires']})}\n\n"
                await asyncio.sleep(3)
                ecoule += 3
        finally:
            _FLUX_ACTIFS -= 1

    return StreamingResponse(generateur(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ───────────── signalements ─────────────

@router.post("/api/communaute/signalements")
async def signaler(request: Request, response: Response):
    u = await _utilisateur(request, response)
    body = await request.json()
    cible_type, motif = str(body.get("cible_type")), str(body.get("motif"))
    if cible_type not in ("post", "commentaire") or motif not in MOTIFS:
        raise HTTPException(400, "Signalement invalide.")
    try:
        cible_id = int(body.get("cible_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Cible invalide.")
    table = "communaute_posts" if cible_type == "post" else "communaute_commentaires"
    if not await fetch_one(f"SELECT 1 AS x FROM {table} WHERE id = %s", (cible_id,)):
        raise HTTPException(404, "Contenu introuvable")
    await _limite("SELECT count(*) AS n FROM communaute_signalements WHERE auteur_id = %s AND cree_le > now() - interval '1 day'", (u["id"],), 20,
                  "Limite quotidienne de signalements atteinte.")
    await execute("INSERT INTO communaute_signalements (cible_type, cible_id, auteur_id, motif, detail) VALUES (%s, %s, %s, %s, %s) "
                  "ON CONFLICT (cible_type, cible_id, auteur_id) DO NOTHING",
                  (cible_type, cible_id, u["id"], motif, _nettoyer(body.get("detail"), 1000) or None))
    n = await fetch_one("SELECT count(DISTINCT auteur_id) AS n FROM communaute_signalements WHERE cible_type = %s AND cible_id = %s AND statut = 'ouvert'", (cible_type, cible_id))
    # Principe de précaution : masquage immédiat pour « mineur », sinon dès SEUIL_MASQUAGE_AUTO signalements distincts.
    if motif == "mineur" or int(n["n"]) >= SEUIL_MASQUAGE_AUTO:
        statut_cible = "masque"
        await execute(f"UPDATE {table} SET statut = %s{', motif_moderation = %s' if cible_type == 'post' else ''} WHERE id = %s AND statut = 'publie'",
                      (statut_cible, "masqué automatiquement après signalement", cible_id) if cible_type == "post" else (statut_cible, cible_id))
        await execute("INSERT INTO communaute_journal_moderation (moderateur, action, cible_type, cible_id, motif) VALUES ('automatique', 'masquer', %s, %s, %s)",
                      (cible_type, cible_id, f"signalement ({motif})"))
    return {"recu": True, "message": "Merci, votre signalement a été transmis à l'équipe."}


# ───────────── droits RGPD ─────────────

@router.get("/api/communaute/mes-donnees")
async def mes_donnees(request: Request, response: Response):
    """Droit d'accès et à la portabilité (RGPD art. 15 et 20) : tout ce que Pelify détient sur ce profil."""
    u = await _utilisateur(request, response)
    profil = await fetch_one("SELECT pseudo, statut, regles_version, consentement_le, cree_le, posts_valides FROM communaute_profils WHERE user_id = %s", (u["id"],))
    export = {
        "genere_le": datetime.utcnow().isoformat() + "Z",
        "profil": dict(profil) if profil else None,
        "publications": [dict(r) for r in await fetch_all("SELECT id, lieu_id, film_id, titre, texte, note, date_visite, statut, cree_le FROM communaute_posts WHERE auteur_id = %s ORDER BY id", (u["id"],))],
        "commentaires": [dict(r) for r in await fetch_all("SELECT id, post_id, texte, statut, cree_le FROM communaute_commentaires WHERE auteur_id = %s ORDER BY id", (u["id"],))],
        "photos": [dict(r) for r in await fetch_all("SELECT id, post_id, mime, taille_octets, statut, cree_le FROM communaute_medias WHERE auteur_id = %s ORDER BY id", (u["id"],))],
        "reactions": [dict(r) for r in await fetch_all("SELECT post_id, cree_le FROM communaute_reactions WHERE user_id = %s", (u["id"],))],
        "signalements_emis": [dict(r) for r in await fetch_all("SELECT cible_type, cible_id, motif, statut, cree_le FROM communaute_signalements WHERE auteur_id = %s", (u["id"],))],
    }
    contenu = json.dumps(export, ensure_ascii=False, default=str, indent=2)
    return Response(contenu, media_type="application/json", headers={"Content-Disposition": 'attachment; filename="pelify-mes-donnees.json"'})


@router.delete("/api/communaute/mon-compte")
async def supprimer_mon_compte(request: Request, response: Response):
    """Droit à l'effacement (RGPD art. 17) : profil, publications, commentaires, réactions et fichiers."""
    u = await _utilisateur(request, response)
    posts = [r["id"] for r in await fetch_all("SELECT id FROM communaute_posts WHERE auteur_id = %s", (u["id"],))]
    rows = await fetch_all("SELECT cle_stockage FROM communaute_medias WHERE auteur_id = %s", (u["id"],))
    if stockage_media.configure():
        await asyncio.gather(*(stockage_media.supprimer_objet(r["cle_stockage"]) for r in rows), return_exceptions=True)
    await execute("DELETE FROM communaute_profils WHERE user_id = %s", (u["id"],))     # cascade sur tout le reste
    return {"supprime": True, "publications_supprimees": len(posts), "photos_supprimees": len(rows)}


# ───────────── modération (équipe) ─────────────

@router.get("/api/communaute/admin/file")
async def file_moderation(request: Request):
    exiger_admin(request)
    attente = await fetch_all(f"{_SELECT_POST} WHERE p.statut IN ('en_attente', 'masque') ORDER BY p.id LIMIT 100")
    signalements = await fetch_all("SELECT cible_type, cible_id, count(*) AS nb, string_agg(DISTINCT motif, ', ') AS motifs, min(cree_le) AS depuis "
                                   "FROM communaute_signalements WHERE statut = 'ouvert' GROUP BY 1, 2 ORDER BY nb DESC, depuis LIMIT 100")
    return {"posts": [{"id": r["id"], "pseudo": r["pseudo"], "statut": r["statut"], "texte": r["texte"][:500], "cree_le": r["cree_le"].isoformat()} for r in attente],
            "signalements": [{**dict(r), "depuis": r["depuis"].isoformat()} for r in signalements]}


@router.post("/api/communaute/admin/decision")
async def decision(request: Request):
    """action : approuver | masquer | supprimer ; chaque décision est journalisée avec son motif (exposé des motifs, DSA)."""
    exiger_admin(request)
    body = await request.json()
    action, cible_type = str(body.get("action")), str(body.get("cible_type"))
    motif = _nettoyer(body.get("motif"), 500)
    if action not in ("approuver", "masquer", "supprimer") or cible_type not in ("post", "commentaire"):
        raise HTTPException(400, "Décision invalide.")
    if action != "approuver" and not motif:
        raise HTTPException(400, "Un motif est obligatoire pour masquer ou supprimer (il sera communiqué à l'auteur).")
    cible_id = int(body.get("cible_id"))
    table = "communaute_posts" if cible_type == "post" else "communaute_commentaires"
    statut = {"approuver": "publie", "masquer": "masque", "supprimer": "supprime"}[action]
    if cible_type == "post":
        await execute("UPDATE communaute_posts SET statut = %s, motif_moderation = %s, modifie_le = now() WHERE id = %s", (statut, motif or None, cible_id))
        if action == "approuver":
            await execute("UPDATE communaute_profils SET posts_valides = posts_valides + 1 WHERE user_id = (SELECT auteur_id FROM communaute_posts WHERE id = %s)", (cible_id,))
        if action == "supprimer":
            await _supprimer_medias_post([cible_id])
    else:
        await execute(f"UPDATE {table} SET statut = %s WHERE id = %s", (statut, cible_id))
    await execute("UPDATE communaute_signalements SET statut = %s, traite_le = now() WHERE cible_type = %s AND cible_id = %s AND statut = 'ouvert'",
                  ("rejete" if action == "approuver" else "traite", cible_type, cible_id))
    await execute("INSERT INTO communaute_journal_moderation (moderateur, action, cible_type, cible_id, motif) VALUES (%s, %s, %s, %s, %s)",
                  (_nettoyer(body.get("moderateur"), 60) or "equipe", action, cible_type, cible_id, motif or None))
    return {"ok": True, "statut": statut}


@router.post("/api/communaute/admin/profil")
async def sanction_profil(request: Request):
    exiger_admin(request)
    body = await request.json()
    statut = str(body.get("statut"))
    if statut not in ("actif", "suspendu", "banni"):
        raise HTTPException(400, "Statut invalide.")
    pseudo = str(body.get("pseudo") or "")
    r = await fetch_one("SELECT user_id FROM communaute_profils WHERE pseudo_norm = %s", (_norm(pseudo),))
    if not r:
        raise HTTPException(404, "Profil introuvable")
    await execute("UPDATE communaute_profils SET statut = %s WHERE user_id = %s", (statut, r["user_id"]))
    await execute("INSERT INTO communaute_journal_moderation (moderateur, action, cible_type, cible_id, motif) VALUES (%s, %s, 'profil', 0, %s)",
                  (_nettoyer(body.get("moderateur"), 60) or "equipe", f"profil_{statut}", f"{pseudo} : {_nettoyer(body.get('motif'), 400)}"))
    return {"ok": True}