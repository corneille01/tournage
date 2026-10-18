"""
backend/stockage_media.py

Stockage des photos de la communauté sur Cloudflare R2 (compatible S3), par URL pré-signée :
le navigateur envoie la photo DIRECTEMENT à R2 (le serveur Pelify ne transporte jamais le fichier).

Variables d'environnement :
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
    MEDIA_PUBLIC_BASE   adresse publique du bucket (ex. https://media.pelify.app)

Sans configuration : `configure()` renvoie False et la communauté fonctionne en texte seul.
Signature AWS SigV4 implémentée ici sans dépendance ; vérifiée contre botocore.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
from urllib.parse import quote

import httpx

REGION, SERVICE = "auto", "s3"


def configure() -> bool:
    return all(os.getenv(k) for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "MEDIA_PUBLIC_BASE"))


def _hote() -> str:
    return f"{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"


def url_publique(cle: str) -> str:
    return f"{os.environ['MEDIA_PUBLIC_BASE'].rstrip('/')}/{quote(cle, safe='/')}"


def _hmac(cle: bytes, msg: str) -> bytes:
    return hmac.new(cle, msg.encode("utf-8"), hashlib.sha256).digest()


def _cle_signature(secret: str, jour: str) -> bytes:
    return _hmac(_hmac(_hmac(_hmac(("AWS4" + secret).encode(), jour), REGION), SERVICE), "aws4_request")


def url_presignee(methode: str, cle: str, expire_s: int = 600, content_type: str | None = None,
                  maintenant: dt.datetime | None = None) -> str:
    """URL pré-signée (SigV4, paramètres dans l'URL). Pour un PUT, le Content-Type est signé : R2 refuse
    tout envoi dont le type ne correspond pas à celui déclaré."""
    ak, sk, bucket = os.environ["R2_ACCESS_KEY_ID"], os.environ["R2_SECRET_ACCESS_KEY"], os.environ["R2_BUCKET"]
    t = maintenant or dt.datetime.now(dt.timezone.utc)
    horodatage, jour = t.strftime("%Y%m%dT%H%M%SZ"), t.strftime("%Y%m%d")
    portee = f"{jour}/{REGION}/{SERVICE}/aws4_request"
    en_tetes = {"host": _hote()}
    if content_type:
        en_tetes["content-type"] = content_type
    signes = ";".join(sorted(en_tetes))
    chemin = f"/{bucket}/{quote(cle, safe='/')}"
    params = {"X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": f"{ak}/{portee}", "X-Amz-Date": horodatage,
              "X-Amz-Expires": str(expire_s), "X-Amz-SignedHeaders": signes}
    requete = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in sorted(params.items()))
    canonique = "\n".join([methode, chemin, requete, "".join(f"{k}:{en_tetes[k]}\n" for k in sorted(en_tetes)), signes, "UNSIGNED-PAYLOAD"])
    a_signer = "\n".join(["AWS4-HMAC-SHA256", horodatage, portee, hashlib.sha256(canonique.encode()).hexdigest()])
    signature = hmac.new(_cle_signature(sk, jour), a_signer.encode(), hashlib.sha256).hexdigest()
    return f"https://{_hote()}{chemin}?{requete}&X-Amz-Signature={signature}"


async def verifier_objet(cle: str) -> dict | None:
    """HEAD sur l'objet : existe-t-il, quelle taille, quel type ? (None si absent ou erreur)."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.head(url_presignee("HEAD", cle, 60))
        if r.status_code != 200:
            return None
        return {"taille": int(r.headers.get("content-length", 0)), "mime": r.headers.get("content-type", "")}
    except Exception:
        return None


async def supprimer_objet(cle: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.delete(url_presignee("DELETE", cle, 60))
        return r.status_code in (200, 204, 404)
    except Exception:
        return False