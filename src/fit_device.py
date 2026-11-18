"""Lecture minimale et réécriture d'appareil dans un fichier FIT, sans dépendance externe.

Sert à l'import des sorties MyWhoosh (`mywhoosh_sync`) : Garmin Connect ne compte une activité
importée dans la charge / le statut d'entraînement que si elle semble venir d'un appareil
Garmin. On remplace donc fabricant/produit de l'appareil créateur par ceux d'un Edge 840.

Choix d'implémentation : patch des octets en place (mêmes tailles de champs) puis recalcul du
CRC de fin de fichier, plutôt que décodage/réencodage complet avec une librairie FIT. Rien
d'autre n'est touché (champs développeur, messages inconnus, compression d'horodatage).

Format FIT : en-tête de 12 ou 14 octets, enregistrements (définitions + données), CRC16 final.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

GARMIN_MANUFACTURER = 1
EDGE_840_PRODUCT = 4024
EDGE_840_SOFTWARE_VERSION = 2019  # 20.19 (échelle 100)

_MSG_FILE_ID = 0
_MSG_SESSION = 18
_MSG_DEVICE_INFO = 23

# Numéros de champs (profil FIT).
_FILE_ID_MANUFACTURER, _FILE_ID_PRODUCT, _FILE_ID_TIME_CREATED = 1, 2, 4
_DEVICE_INDEX, _DEVICE_MANUFACTURER, _DEVICE_PRODUCT, _DEVICE_SOFTWARE = 0, 2, 4, 5
_SESSION_START_TIME, _SESSION_SPORT, _SESSION_ELAPSED = 2, 5, 7

_FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)

_CRC_TABLE = (
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
)


class FitFormatError(ValueError):
    """Fichier qui n'est pas un FIT lisible."""


def fit_crc(data: bytes, crc: int = 0) -> int:
    for byte in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[byte & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc


@dataclass
class _FieldLoc:
    offset: int  # position absolue dans le fichier
    size: int
    little_endian: bool


@dataclass
class _Message:
    global_num: int
    fields: dict[int, _FieldLoc] = field(default_factory=dict)
    local: int = 0
    start: int = 0  # octet d'en-tête d'enregistrement
    fields_end: int = 0  # fin des champs standard (début des champs développeur)
    end: int = 0
    timestamp: int | None = None  # secondes FIT, horodatage compressé résolu


@dataclass
class _Definition:
    local: int
    global_num: int
    little_endian: bool
    field_nums: list[int]
    start: int
    fields_end: int  # fin des triplets de champs standard
    end: int


@dataclass
class FitSummary:
    """Ce dont l'import a besoin : heure de départ (UTC), durée, sport."""

    start_utc: datetime | None
    elapsed_s: float | None
    sport: int | None


_FIELD_TIMESTAMP = 253


def _parse_chunks(data: bytes) -> tuple[int, int, list[_Message | _Definition]]:
    """Renvoie (taille d'en-tête, fin des données, enregistrements dans l'ordre)."""
    if len(data) < 14:
        raise FitFormatError("fichier trop court")
    header_size = data[0]
    if header_size not in (12, 14) or data[8:12] != b".FIT":
        raise FitFormatError("en-tête FIT absent")
    data_size = struct.unpack_from("<I", data, 4)[0]
    end = header_size + data_size
    if end + 2 > len(data):
        raise FitFormatError("fichier tronqué")

    definitions: dict[int, tuple[_Definition, list[tuple[int, int]], int]] = {}
    chunks: list[_Message | _Definition] = []
    last_ts: int | None = None
    pos = header_size
    try:
        while pos < end:
            start = pos
            rec = data[pos]
            pos += 1
            compressed_offset = None
            if rec & 0x80:  # données à horodatage compressé
                local = (rec >> 5) & 0x3
                compressed_offset = rec & 0x1F
                is_definition = False
            else:
                local = rec & 0x0F
                is_definition = bool(rec & 0x40)
            if is_definition:
                little = data[pos + 1] == 0
                global_num = struct.unpack_from("<H" if little else ">H", data, pos + 2)[0]
                n_fields = data[pos + 4]
                pos += 5
                fields = [(data[pos + 3 * i], data[pos + 3 * i + 1]) for i in range(n_fields)]
                pos += 3 * n_fields
                fields_end = pos
                dev_size = 0
                if rec & 0x20:  # champs développeur
                    n_dev = data[pos]
                    pos += 1
                    dev_size = sum(data[pos + 3 * i + 1] for i in range(n_dev))
                    pos += 3 * n_dev
                d = _Definition(local, global_num, little, [n for n, _ in fields], start, fields_end, pos)
                definitions[local] = (d, fields, dev_size)
                chunks.append(d)
                continue
            if local not in definitions:
                raise FitFormatError(f"message sans définition (type local {local})")
            d, fields, dev_size = definitions[local]
            msg = _Message(d.global_num, local=local, start=start)
            for num, size in fields:
                msg.fields[num] = _FieldLoc(pos, size, d.little_endian)
                pos += size
            msg.fields_end = pos
            pos += dev_size
            msg.end = pos
            ts = _read_uint(data, msg.fields.get(_FIELD_TIMESTAMP))
            if ts is not None:
                last_ts = ts
            elif compressed_offset is not None and last_ts is not None:
                ts = (last_ts & ~0x1F) + compressed_offset
                if compressed_offset < (last_ts & 0x1F):
                    ts += 0x20
                last_ts = ts
            msg.timestamp = ts
            chunks.append(msg)
    except (IndexError, struct.error) as exc:
        raise FitFormatError("enregistrement tronqué") from exc
    if pos != end:
        raise FitFormatError("enregistrements incohérents avec la taille annoncée")
    return header_size, end, chunks


def _parse(data: bytes) -> tuple[int, int, list[_Message]]:
    header_size, end, chunks = _parse_chunks(data)
    return header_size, end, [c for c in chunks if isinstance(c, _Message)]


def _read_uint(data: bytes, loc: _FieldLoc | None) -> int | None:
    if loc is None or loc.size not in (1, 2, 4):
        return None
    fmt = {1: "B", 2: "H", 4: "I"}[loc.size]
    value = struct.unpack_from(("<" if loc.little_endian else ">") + fmt, data, loc.offset)[0]
    invalid = {1: 0xFF, 2: 0xFFFF, 4: 0xFFFFFFFF}[loc.size]
    return None if value == invalid else value


def _write_uint16(buf: bytearray, loc: _FieldLoc | None, value: int) -> bool:
    if loc is None or loc.size != 2:
        return False
    struct.pack_into("<H" if loc.little_endian else ">H", buf, loc.offset, value)
    return True


def summarize(data: bytes) -> FitSummary:
    _, _, messages = _parse(data)
    session = next((m for m in messages if m.global_num == _MSG_SESSION), None)
    file_id = next((m for m in messages if m.global_num == _MSG_FILE_ID), None)
    start_raw = _read_uint(data, session.fields.get(_SESSION_START_TIME)) if session else None
    if start_raw is None and file_id is not None:
        start_raw = _read_uint(data, file_id.fields.get(_FILE_ID_TIME_CREATED))
    elapsed_raw = _read_uint(data, session.fields.get(_SESSION_ELAPSED)) if session else None
    return FitSummary(
        start_utc=_FIT_EPOCH + timedelta(seconds=start_raw) if start_raw is not None else None,
        elapsed_s=elapsed_raw / 1000 if elapsed_raw is not None else None,
        sport=_read_uint(data, session.fields.get(_SESSION_SPORT)) if session else None,
    )


def rewrite_as_edge_840(data: bytes) -> bytes:
    """Copie du FIT où l'appareil créateur (file_id + device_info de l'index 0) devient un
    Garmin Edge 840. Les autres appareils (home trainer, capteurs) sont laissés intacts."""
    header_size, end, messages = _parse(data)
    buf = bytearray(data)
    patched_file_id = False
    for msg in messages:
        if msg.global_num == _MSG_FILE_ID:
            ok_m = _write_uint16(buf, msg.fields.get(_FILE_ID_MANUFACTURER), GARMIN_MANUFACTURER)
            ok_p = _write_uint16(buf, msg.fields.get(_FILE_ID_PRODUCT), EDGE_840_PRODUCT)
            patched_file_id = patched_file_id or (ok_m and ok_p)
        elif msg.global_num == _MSG_DEVICE_INFO:
            index_loc = msg.fields.get(_DEVICE_INDEX)
            index = _read_uint(data, index_loc) if index_loc else 0
            if index not in (0, None) and index_loc is not None:
                continue
            _write_uint16(buf, msg.fields.get(_DEVICE_MANUFACTURER), GARMIN_MANUFACTURER)
            _write_uint16(buf, msg.fields.get(_DEVICE_PRODUCT), EDGE_840_PRODUCT)
            _write_uint16(buf, msg.fields.get(_DEVICE_SOFTWARE), EDGE_840_SOFTWARE_VERSION)
    if not patched_file_id:
        raise FitFormatError("message file_id sans champs fabricant/produit sur 16 bits")
    struct.pack_into("<H", buf, end, fit_crc(bytes(buf[:end])))
    return bytes(buf)


def read_creator(data: bytes) -> tuple[int | None, int | None]:
    """(fabricant, produit) du message file_id — utile aux tests et au diagnostic."""
    _, _, messages = _parse(data)
    file_id = next((m for m in messages if m.global_num == _MSG_FILE_ID), None)
    if file_id is None:
        return None, None
    return (
        _read_uint(data, file_id.fields.get(_FILE_ID_MANUFACTURER)),
        _read_uint(data, file_id.fields.get(_FILE_ID_PRODUCT)),
    )


# -- fusion de fréquence cardiaque ---------------------------------------------
#
# Sert à `mywhoosh_merge_hr` : la montre enregistre la FC (« Vélo d'intérieur »), MyWhoosh la
# puissance, la cadence et la distance virtuelle. On injecte la FC de la montre dans le FIT
# MyWhoosh, seconde par seconde, ainsi que FC moyenne/max dans les tours et la session, pour
# que Garmin calcule charge et effet d'entraînement à partir d'une seule activité complète.

_MSG_LAP = 19
_MSG_RECORD = 20
_RECORD_HR = 3
_LAP_AVG_HR, _LAP_MAX_HR = 15, 16
_SESSION_AVG_HR, _SESSION_MAX_HR = 16, 17
_START_TIME = 2  # lap et session
_UINT8 = 0x02
_INVALID_UINT8 = 0xFF
# Les montres en enregistrement « intelligent » n'écrivent la FC que toutes les 1 à 8 s.
_HR_MATCH_TOLERANCE_S = 5


def heart_rate_series(data: bytes) -> dict[int, int]:
    """FC par seconde FIT, lue dans les messages `record`."""
    _, _, messages = _parse(data)
    series: dict[int, int] = {}
    for m in messages:
        if m.global_num == _MSG_RECORD and m.timestamp is not None:
            hr = _read_uint(data, m.fields.get(_RECORD_HR))
            if hr:
                series[m.timestamp] = hr
    return series


def _hr_at(series: dict[int, int], ts: int | None) -> int | None:
    if ts is None:
        return None
    for delta in range(_HR_MATCH_TOLERANCE_S + 1):
        for candidate in (ts - delta, ts + delta):
            if candidate in series:
                return series[candidate]
    return None


def _window_stats(series: dict[int, int], start: int | None, end: int | None) -> tuple[int | None, int | None]:
    if start is None or end is None:
        return None, None
    values = [hr for ts, hr in series.items() if start <= ts <= end]
    if not values:
        return None, None
    return round(sum(values) / len(values)), max(values)


def inject_heart_rate(data: bytes, series: dict[int, int]) -> tuple[bytes, int]:
    """Copie du FIT avec la FC de `series` dans chaque `record` (± 5 s) et FC moyenne/max
    dans chaque tour et la session. Les champs absents des définitions sont ajoutés (uint8),
    ceux déjà présents sont écrasés. Renvoie (fichier, nombre de records renseignés)."""
    header_size, end, chunks = _parse_chunks(data)

    # Champs FC à garantir par type de message, et valeur à écrire pour un message donné.
    def values_for(msg: _Message) -> dict[int, int | None]:
        if msg.global_num == _MSG_RECORD:
            return {_RECORD_HR: _hr_at(series, msg.timestamp)}
        if msg.global_num in (_MSG_LAP, _MSG_SESSION):
            start = _read_uint(data, msg.fields.get(_START_TIME))
            avg, mx = _window_stats(series, start, msg.timestamp)
            if msg.global_num == _MSG_LAP:
                return {_LAP_AVG_HR: avg, _LAP_MAX_HR: mx}
            return {_SESSION_AVG_HR: avg, _SESSION_MAX_HR: mx}
        return {}

    wanted = {
        _MSG_RECORD: [_RECORD_HR],
        _MSG_LAP: [_LAP_AVG_HR, _LAP_MAX_HR],
        _MSG_SESSION: [_SESSION_AVG_HR, _SESSION_MAX_HR],
    }
    out = bytearray(data[:header_size])
    added: dict[int, list[int]] = {}  # type local -> champs ajoutés à sa définition courante
    filled = 0
    for chunk in chunks:
        if isinstance(chunk, _Definition):
            extra = [n for n in wanted.get(chunk.global_num, []) if n not in chunk.field_nums]
            added[chunk.local] = extra
            raw = bytearray(data[chunk.start : chunk.end])
            if extra:
                raw[5] += len(extra)  # nombre de champs standard
                insert_at = chunk.fields_end - chunk.start
                raw[insert_at:insert_at] = b"".join(struct.pack("BBB", n, 1, _UINT8) for n in extra)
            out += raw
            continue
        values = values_for(chunk)
        raw = bytearray(data[chunk.start : chunk.end])
        for num, value in values.items():
            loc = chunk.fields.get(num)
            # Écrase aussi les valeurs existantes sans FC montre : MyWhoosh écrit 0 bpm
            # quand aucun capteur n'est appairé, ce qui fausserait moyennes et charge.
            if loc is not None and loc.size == 1:
                raw[loc.offset - chunk.start] = _INVALID_UINT8 if value is None else value
        extra = added.get(chunk.local, [])
        if extra:
            insert_at = chunk.fields_end - chunk.start
            raw[insert_at:insert_at] = bytes(
                _INVALID_UINT8 if values.get(n) is None else values[n] for n in extra
            )
        if chunk.global_num == _MSG_RECORD and values.get(_RECORD_HR) is not None:
            filled += 1
        out += raw

    struct.pack_into("<I", out, 4, len(out) - header_size)
    if header_size == 14:
        struct.pack_into("<H", out, 12, fit_crc(bytes(out[:12])))
    out += struct.pack("<H", fit_crc(bytes(out)))
    return bytes(out), filled


# -- puissance ------------------------------------------------------------------------

_RECORD_POWER = 7
_MAX_GAP_FILL_S = 5  # trou plus long : compté à 0 W (pause, capteur décroché)


def power_series(data: bytes) -> list[int]:
    """Puissance seconde par seconde (W), des `record` du FIT. Les trous courts (≤ 5 s,
    enregistrement espacé) reprennent la dernière valeur ; au-delà, 0 W."""
    _, _, messages = _parse(data)
    points: dict[int, int] = {}
    for m in messages:
        if m.global_num == _MSG_RECORD and m.timestamp is not None:
            power = _read_uint(data, m.fields.get(_RECORD_POWER))
            if power is not None:
                points[m.timestamp] = power
    if not points:
        return []
    start, end = min(points), max(points)
    series: list[int] = []
    last, last_ts = 0, start
    for ts in range(start, end + 1):
        if ts in points:
            last, last_ts = points[ts], ts
            series.append(last)
        else:
            series.append(last if ts - last_ts <= _MAX_GAP_FILL_S else 0)
    return series


def best_mean_power(series: list[int], window_s: int) -> float | None:
    """Meilleure puissance moyenne sur `window_s` secondes consécutives."""
    if window_s <= 0 or len(series) < window_s:
        return None
    current = sum(series[:window_s])
    best = current
    for i in range(window_s, len(series)):
        current += series[i] - series[i - window_s]
        best = max(best, current)
    return best / window_s
