"""Client MyWhoosh minimal : connexion, liste des sorties, téléchargement du FIT.

MyWhoosh n'a pas d'API publique : ces trois appels sont ceux de l'app mobile, repris du
projet communautaire marcelorodrigo/mywhoosh-to-garmin (relu avant reprise). Non documentés,
ils peuvent changer sans préavis : toute réponse inattendue lève `MyWhooshError` avec un
message explicite plutôt que de deviner.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LOGIN_URL = "https://services.mywhoosh.com/http-service/api/login"
ACTIVITIES_URL = "https://service14.mywhoosh.com/v2/rider/profile/activities"
DOWNLOAD_URL = "https://service14.mywhoosh.com/v2/rider/profile/download-activity-file"
# Workout Builder (workout.mywhoosh.com) : même jeton que l'app mobile.
MY_WORKOUTS_URL = "https://coaching.mywhoosh.com/api/v2/workout-builder/my-workouts"
WORKOUT_UPLOAD_URL = "https://coaching.mywhoosh.com/api/v2/client/custom-workout-upload"
_TIMEOUT_S = 30


class MyWhooshError(Exception):
    """Échec d'accès à MyWhoosh. `error_type` : "auth" | "network" | "unknown"."""

    def __init__(self, message: str, error_type: str = "network") -> None:
        super().__init__(message)
        self.error_type = error_type


def _http(url: str, *, payload: dict[str, Any] | None = None, token: str | None = None) -> bytes:
    headers = {"Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        kind = "auth" if exc.code in (401, 403) else "network"
        raise MyWhooshError(f"HTTP {exc.code} sur {url.split('?')[0]}", kind) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MyWhooshError(f"MyWhoosh injoignable : {exc}") from exc


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise MyWhooshError("réponse MyWhoosh non JSON", "unknown") from exc


_LOGIN_TYPE_DEVICE_TRUST = 6


def load_device_id(path: Path) -> str:
    """Identifiant d'appareil stable, créé au premier appel. MyWhoosh exige de valider chaque
    nouvel appareil (« Device trust verification required ») : un identifiant tiré au hasard
    à chaque démarrage redemanderait la validation à chaque fois."""
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    value = str(uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return value


class MyWhooshClient:
    def __init__(self, email: str, password: str, device_id: str | None = None) -> None:
        self._email = email
        self._password = password
        self._device_id = device_id or str(uuid.uuid4())
        self._token: str | None = None
        self._whoosh_id: str | None = None

    def _login(self) -> None:
        data = _json(
            _http(
                LOGIN_URL,
                payload={
                    "Username": self._email,
                    "Password": self._password,
                    "Platform": "Android",
                    "Action": 1001,
                    "CorrelationId": str(uuid.uuid4()),
                    "DeviceId": self._device_id,
                    "Authorization": "",
                },
            )
        )
        if isinstance(data, dict) and data.get("LoginType") == _LOGIN_TYPE_DEVICE_TRUST:
            raise MyWhooshError(
                "MyWhoosh demande de valider ce nouvel appareil (identifiant "
                f"{self._device_id}) : suivre la vérification reçue par e-mail, puis relancer",
                "auth",
            )
        if not isinstance(data, dict) or not data.get("Success") or not data.get("AccessToken"):
            message = data.get("Message") if isinstance(data, dict) else None
            raise MyWhooshError(f"connexion MyWhoosh refusée : {message or 'raison inconnue'}", "auth")
        self._token = data["AccessToken"]
        self._whoosh_id = data.get("WhooshId")

    def _authed(self, url: str, payload: dict[str, Any]) -> Any:
        if self._token is None:
            self._login()
        try:
            return _json(_http(url, payload=payload, token=self._token))
        except MyWhooshError as exc:
            if exc.error_type != "auth":
                raise
            self._login()  # jeton expiré : une seule reconnexion
            return _json(_http(url, payload=payload, token=self._token))

    def list_activities(self, limit: int = 10) -> list[dict[str, Any]]:
        """Sorties les plus récentes d'abord (dicts bruts de MyWhoosh)."""
        data = self._authed(ACTIVITIES_URL, {"page": 1, "limit": limit, "sortDate": "DESC"})
        if isinstance(data, list):
            return [a for a in data if isinstance(a, dict)]
        if isinstance(data, dict):
            inner = data.get("data")
            if isinstance(inner, dict) and isinstance(inner.get("results"), list):
                return [a for a in inner["results"] if isinstance(a, dict)]
            if isinstance(inner, list):
                return [a for a in inner if isinstance(a, dict)]
        raise MyWhooshError("format de la liste des sorties MyWhoosh inattendu", "unknown")

    def _user_id(self) -> str:
        """`userId` lu dans le jeton (JWT), comme le fait le Workout Builder."""
        if self._token is None:
            self._login()
        try:
            payload = self._token.split(".")[1]  # type: ignore[union-attr]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (IndexError, ValueError) as exc:
            raise MyWhooshError("jeton MyWhoosh illisible", "unknown") from exc
        user_id = claims.get("userId") or claims.get("UserId")
        if not user_id:
            raise MyWhooshError("identifiant utilisateur absent du jeton MyWhoosh", "unknown")
        return str(user_id)

    def _authed_get(self, url: str) -> Any:
        if self._token is None:
            self._login()
        try:
            return _json(_http(url, token=self._token))
        except MyWhooshError as exc:
            if exc.error_type != "auth":
                raise
            self._login()
            return _json(_http(url, token=self._token))

    def list_custom_workouts(self, sport: int = 0) -> list[dict[str, Any]]:
        """Séances perso du dossier « MyWorkout » (0 = vélo)."""
        data = self._authed_get(f"{MY_WORKOUTS_URL}/{sport}")
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise MyWhooshError("format de la liste des séances MyWhoosh inattendu", "unknown")
        return [w for w in items if isinstance(w, dict)]

    def upload_custom_workout(self, workout: dict[str, Any], sport: int = 0) -> Any:
        """Enregistre une séance dans « MyWorkout » (appel du bouton d'export du Workout Builder)."""
        payload = {"UserId": self._user_id(), "SportsModeType": sport, "WorkoutsData": [workout]}
        data = self._authed(WORKOUT_UPLOAD_URL, payload)
        if isinstance(data, dict) and data.get("status") is False:
            raise MyWhooshError(f"séance refusée par MyWhoosh : {data.get('message') or 'raison inconnue'}", "unknown")
        return data

    def download_fit(self, activity: dict[str, Any]) -> bytes:
        file_id = activity.get("activityFileId")
        if not file_id:
            raise MyWhooshError("sortie MyWhoosh sans fichier FIT (activityFileId absent)", "unknown")
        if self._whoosh_id is None:
            self._login()
        data = self._authed(DOWNLOAD_URL, {"key": self._whoosh_id, "fileId": file_id})
        url = data.get("data") if isinstance(data, dict) else None
        if not isinstance(url, str) or not url.startswith("https://"):
            raise MyWhooshError("lien de téléchargement MyWhoosh absent", "unknown")
        return _http(url)
