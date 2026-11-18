"""Tests de l'import MyWhoosh : lecture/réécriture FIT, détection de chevauchement, tool
`mywhoosh_sync` (MyWhoosh et Garmin mockés, aucun appel réseau)."""

from __future__ import annotations

import struct
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src import fit_device as fd
from src.garmin_client import GarminClient, GarminUnavailableError
from src.mywhoosh_client import MyWhooshClient, MyWhooshError
from src.tools.mywhoosh_tools import build_mywhoosh_tools, find_overlapping

START = datetime(2026, 11, 17, 18, 0, tzinfo=timezone.utc)
FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)


def _definition(local: int, global_num: int, fields: list[tuple[int, int, int]], big_endian: bool = False) -> bytes:
    arch = 1 if big_endian else 0
    head = struct.pack(">BBBHB" if big_endian else "<BBBHB", 0x40 | local, 0, arch, global_num, len(fields))
    return head + b"".join(struct.pack("BBB", *f) for f in fields)


def make_fit(*, manufacturer: int = 260, product: int = 0, elapsed_s: int = 2139) -> bytes:
    """FIT synthétique proche d'une sortie MyWhoosh : file_id, deux device_info (créateur +
    home trainer), une session en big-endian et un enregistrement à horodatage compressé."""
    ts = int((START - FIT_EPOCH).total_seconds())
    body = b""
    body += _definition(0, 0, [(0, 1, 0x00), (1, 2, 0x84), (2, 2, 0x84), (4, 4, 0x86)])
    body += struct.pack("<BBHHI", 0, 4, manufacturer, product, ts)
    body += _definition(1, 23, [(0, 1, 0x02), (2, 2, 0x84), (4, 2, 0x84), (5, 2, 0x84)])
    body += struct.pack("<BBHHH", 1, 0, manufacturer, product, 100)
    body += struct.pack("<BBHHH", 1, 1, 89, 7, 300)  # home trainer (Wahoo), à ne pas toucher
    body += _definition(2, 18, [(2, 4, 0x86), (5, 1, 0x00), (7, 4, 0x86)], big_endian=True)
    body += struct.pack(">BIBI", 2, ts, 2, elapsed_s * 1000)
    body += _definition(3, 20, [(7, 2, 0x84)])
    body += struct.pack("<BH", 0x80 | (3 << 5) | 5, 159)  # puissance, horodatage compressé
    header = struct.pack("<BBHI4s", 14, 0x20, 2200, len(body), b".FIT")
    header += struct.pack("<H", fd.fit_crc(header))
    data = header + body
    return data + struct.pack("<H", fd.fit_crc(data))


def _device_infos(data: bytes) -> list[tuple[int, int, int, int]]:
    _, _, messages = fd._parse(data)
    out = []
    for m in messages:
        if m.global_num == 23:
            out.append(tuple(fd._read_uint(data, m.fields[n]) for n in (0, 2, 4, 5)))
    return out


# -- FIT -------------------------------------------------------------------


def test_summarize_reads_session_start_elapsed_and_sport() -> None:
    summary = fd.summarize(make_fit())
    assert summary.start_utc == START
    assert summary.elapsed_s == 2139
    assert summary.sport == 2  # cyclisme


def test_rewrite_patches_creator_only_and_keeps_file_valid() -> None:
    original = make_fit()
    patched = fd.rewrite_as_edge_840(original)

    assert len(patched) == len(original)
    assert fd.read_creator(patched) == (fd.GARMIN_MANUFACTURER, fd.EDGE_840_PRODUCT)
    assert _device_infos(patched) == [
        (0, fd.GARMIN_MANUFACTURER, fd.EDGE_840_PRODUCT, fd.EDGE_840_SOFTWARE_VERSION),
        (1, 89, 7, 300),
    ]
    # CRC final valide : le CRC de tout le fichier (CRC inclus) vaut 0.
    assert fd.fit_crc(patched) == 0
    # Données d'effort inchangées.
    assert fd.summarize(patched) == fd.summarize(original)


def test_rejects_non_fit_and_truncated_files() -> None:
    with pytest.raises(fd.FitFormatError):
        fd.summarize(b"<html>pas un fit</html>")
    with pytest.raises(fd.FitFormatError):
        fd.summarize(make_fit()[:-10])


# -- chevauchement -----------------------------------------------------------


def _garmin_act(start: datetime, minutes: int, type_key: str = "indoor_cycling") -> dict:
    return {
        "activityId": 42,
        "activityName": "Vélo d'intérieur",
        "startTimeGMT": start.strftime("%Y-%m-%d %H:%M:%S"),
        "duration": minutes * 60.0,
        "activityType": {"typeKey": type_key},
    }


def test_overlap_detects_watch_recording_despite_different_name() -> None:
    end = START + timedelta(minutes=36)
    found = find_overlapping([_garmin_act(START + timedelta(minutes=2), 30)], START, end)
    assert found is not None and found["activity_id"] == 42 and found["type"] == "velo"


def test_overlap_ignores_activities_outside_margin() -> None:
    end = START + timedelta(minutes=36)
    assert find_overlapping([_garmin_act(START - timedelta(hours=2), 60)], START, end) is None
    assert find_overlapping([_garmin_act(end + timedelta(minutes=10), 30)], START, end) is None


# -- tool -----------------------------------------------------------------


@pytest.fixture
def garmin() -> MagicMock:
    g = MagicMock(spec=GarminClient)
    g.get_activities_by_date.return_value = []
    g.upload_activity_file.return_value = {"detailedImportResult": {"successes": [{"internalId": 999}]}}
    return g


@pytest.fixture
def mywhoosh() -> MagicMock:
    m = MagicMock(spec=MyWhooshClient)
    m.list_activities.return_value = [{"name": "MyWhoosh - Tour de Corse", "activityFileId": "f1"}]
    m.download_fit.return_value = make_fit()
    return m


async def test_sync_uploads_edge_840_file(garmin: MagicMock, mywhoosh: MagicMock) -> None:
    uploaded: list[bytes] = []
    garmin.upload_activity_file.side_effect = lambda path: (
        uploaded.append(open(path, "rb").read()) or {"detailedImportResult": {"successes": [{"internalId": 999}]}}
    )
    tool = build_mywhoosh_tools(garmin, mywhoosh)["mywhoosh_sync"]
    result = await tool()

    assert result["status"] == "ok"
    ride = result["sorties"][0]
    assert ride["statut"] == "importe" and ride["activity_id"] == 999
    assert ride["nom"] == "MyWhoosh - Tour de Corse" and ride["duree_minutes"] == 36
    assert ride["debut"].startswith("2026-11-17T19:00")  # heure de Paris
    assert fd.read_creator(uploaded[0]) == (fd.GARMIN_MANUFACTURER, fd.EDGE_840_PRODUCT)


async def test_sync_skips_ride_already_recorded_by_watch(garmin: MagicMock, mywhoosh: MagicMock) -> None:
    garmin.get_activities_by_date.return_value = [_garmin_act(START, 35)]
    tool = build_mywhoosh_tools(garmin, mywhoosh)["mywhoosh_sync"]

    result = await tool()
    assert result["sorties"][0]["statut"] == "deja_present"
    garmin.upload_activity_file.assert_not_called()

    forced = await tool(force=True)
    assert forced["sorties"][0]["statut"] == "importe"


async def test_activity_id_recovered_when_garmin_processes_async(garmin: MagicMock, mywhoosh: MagicMock) -> None:
    garmin.upload_activity_file.return_value = {"detailedImportResult": {"successes": [], "failures": []}}
    garmin.get_activities_by_date.side_effect = [[], [_garmin_act(START, 36, "virtual_ride")]]
    result = await build_mywhoosh_tools(garmin, mywhoosh)["mywhoosh_sync"]()
    assert result["sorties"][0] == {**result["sorties"][0], "statut": "importe", "activity_id": 42}


async def test_dry_run_never_uploads(garmin: MagicMock, mywhoosh: MagicMock) -> None:
    result = await build_mywhoosh_tools(garmin, mywhoosh)["mywhoosh_sync"](dry_run=True)
    assert result["sorties"][0]["statut"] == "a_importer"
    garmin.upload_activity_file.assert_not_called()


async def test_garmin_409_reported_as_already_present(garmin: MagicMock, mywhoosh: MagicMock) -> None:
    garmin.upload_activity_file.side_effect = GarminUnavailableError("API Error 409 - Conflict")
    result = await build_mywhoosh_tools(garmin, mywhoosh)["mywhoosh_sync"]()
    assert result["sorties"][0]["statut"] == "deja_present"


async def test_not_configured_and_mywhoosh_errors(garmin: MagicMock, mywhoosh: MagicMock) -> None:
    missing = await build_mywhoosh_tools(garmin, None)["mywhoosh_sync"]()
    assert missing["status"] == "error" and "MYWHOOSH_EMAIL" in missing["message"]

    mywhoosh.list_activities.side_effect = MyWhooshError("connexion MyWhoosh refusée", "auth")
    failed = await build_mywhoosh_tools(garmin, mywhoosh)["mywhoosh_sync"]()
    assert failed["status"] == "error" and failed["error_type"] == "auth"

    bad = await build_mywhoosh_tools(garmin, mywhoosh)["mywhoosh_sync"](count=50)
    assert bad["error_type"] == "invalid_input"


# -- fusion de FC ---------------------------------------------------------------


def make_ride_fit(seconds: int = 120) -> bytes:
    """Sortie MyWhoosh : records puissance seule (timestamp complet puis compressé),
    un tour et une session sans FC."""
    ts0 = int((START - FIT_EPOCH).total_seconds())
    body = _definition(0, 0, [(0, 1, 0x00), (1, 2, 0x84), (2, 2, 0x84), (4, 4, 0x86)])
    body += struct.pack("<BBHHI", 0, 4, 260, 0, ts0)
    body += _definition(1, 20, [(253, 4, 0x86), (7, 2, 0x84)])
    body += _definition(2, 20, [(7, 2, 0x84)])
    for i in range(seconds):
        if i % 10 == 0:
            body += struct.pack("<BIH", 1, ts0 + i, 150 + i)
        else:
            body += struct.pack("<BH", 0x80 | (2 << 5) | ((ts0 + i) & 0x1F), 150 + i)
    body += _definition(3, 19, [(253, 4, 0x86), (2, 4, 0x86)])
    body += struct.pack("<BII", 3, ts0 + seconds - 1, ts0)
    body += _definition(4, 18, [(253, 4, 0x86), (2, 4, 0x86), (5, 1, 0x00), (7, 4, 0x86)])
    body += struct.pack("<BIIBI", 4, ts0 + seconds - 1, ts0, 2, seconds * 1000)
    header = struct.pack("<BBHI4s", 14, 0x20, 2200, len(body), b".FIT")
    header += struct.pack("<H", fd.fit_crc(header))
    data = header + body
    return data + struct.pack("<H", fd.fit_crc(data))


def make_watch_fit(seconds: int = 120, offset_s: int = 3) -> bytes:
    """Activité montre « Vélo d'intérieur » : FC seule, démarrée `offset_s` après MyWhoosh."""
    ts0 = int((START - FIT_EPOCH).total_seconds()) + offset_s
    body = _definition(0, 0, [(0, 1, 0x00), (1, 2, 0x84), (2, 2, 0x84), (4, 4, 0x86)])
    body += struct.pack("<BBHHI", 0, 4, 1, 4257, ts0)
    body += _definition(1, 20, [(253, 4, 0x86), (3, 1, 0x02)])
    for i in range(seconds):
        body += struct.pack("<BIB", 1, ts0 + i, 100 + i % 50)
    body += _definition(2, 18, [(253, 4, 0x86), (2, 4, 0x86), (5, 1, 0x00), (7, 4, 0x86)])
    body += struct.pack("<BIIBI", 2, ts0 + seconds - 1, ts0, 2, seconds * 1000)
    header = struct.pack("<BBHI4s", 14, 0x20, 2200, len(body), b".FIT")
    header += struct.pack("<H", fd.fit_crc(header))
    data = header + body
    return data + struct.pack("<H", fd.fit_crc(data))


def _records(data: bytes) -> list[tuple[int | None, int | None, int | None]]:
    _, _, messages = fd._parse(data)
    return [
        (m.timestamp, fd._read_uint(data, m.fields.get(7)), fd._read_uint(data, m.fields.get(3)))
        for m in messages
        if m.global_num == 20
    ]


def test_inject_heart_rate_aligns_by_timestamp_and_fills_summaries() -> None:
    ride, watch = make_ride_fit(), make_watch_fit(offset_s=8)
    series = fd.heart_rate_series(watch)
    merged, filled = fd.inject_heart_rate(ride, series)

    assert fd.fit_crc(merged) == 0
    records = _records(merged)
    assert [p for _, p, _ in records] == [p for _, p, _ in _records(ride)]  # puissance intacte
    # Montre démarrée 8 s plus tard : FC prise à ±5 s, les 2 premières secondes n'en ont pas.
    assert records[0][2] is None and records[2][2] is None and records[3][2] == 100
    assert records[50][2] == series[records[50][0]]
    assert filled == len(records) - 3

    _, _, messages = fd._parse(merged)
    session = next(m for m in messages if m.global_num == 18)
    lap = next(m for m in messages if m.global_num == 19)
    assert fd._read_uint(merged, session.fields[17]) == 149  # FC max
    assert 100 < fd._read_uint(merged, session.fields[16]) < 149  # FC moyenne
    assert fd._read_uint(merged, lap.fields[16]) == 149
    assert fd.summarize(merged) == fd.summarize(ride)
    # Reste réécrivable en Edge 840 (chaîne complète de la fusion).
    assert fd.read_creator(fd.rewrite_as_edge_840(merged))[1] == fd.EDGE_840_PRODUCT


async def test_merge_tool_uploads_merged_file_and_deletes_only_on_request(
    garmin: MagicMock, mywhoosh: MagicMock
) -> None:
    mywhoosh.download_fit.return_value = make_ride_fit()
    garmin.download_activity_fit.return_value = make_watch_fit()
    uploaded: list[bytes] = []
    garmin.upload_activity_file.side_effect = lambda path: (
        uploaded.append(open(path, "rb").read()) or {"detailedImportResult": {"successes": [{"internalId": 7}]}}
    )
    tool = build_mywhoosh_tools(garmin, mywhoosh)["mywhoosh_merge_hr"]

    kept = await tool(garmin_activity_id=42)
    assert kept["status"] == "ok" and kept["activity_id"] == 7 and kept["original_supprime"] is False
    garmin.delete_activity.assert_not_called()
    assert fd.heart_rate_series(uploaded[0])  # la FC est bien dans le fichier importé
    assert fd.read_creator(uploaded[0]) == (fd.GARMIN_MANUFACTURER, fd.EDGE_840_PRODUCT)

    deleted = await tool(garmin_activity_id=42, delete_original=True)
    assert deleted["original_supprime"] is True
    garmin.delete_activity.assert_called_once_with(42)


async def test_merge_tool_refuses_when_no_ride_overlaps(garmin: MagicMock, mywhoosh: MagicMock) -> None:
    mywhoosh.download_fit.return_value = make_ride_fit()
    garmin.download_activity_fit.return_value = make_watch_fit(offset_s=3 * 3600)
    result = await build_mywhoosh_tools(garmin, mywhoosh)["mywhoosh_merge_hr"](garmin_activity_id=42)
    assert result["status"] == "error"
    garmin.upload_activity_file.assert_not_called()


# -- séances MyWhoosh ---------------------------------------------------------------

from src import mywhoosh_workout as mw

THRESHOLD_BLOCKS = [
    {"type": "warmup", "duration_s": 600, "start_pct": 50, "end_pct": 75, "message": "On s'échauffe"},
    {"type": "intervals", "repeat": 3, "on_s": 600, "on_pct": 95, "off_s": 300, "off_pct": 55, "cadence": 90},
    {"type": "steady", "duration_s": 300, "power_pct": 65},
    {"type": "cooldown", "duration_s": 300, "start_pct": 60, "end_pct": 40},
]


def test_build_workout_matches_workout_builder_format() -> None:
    w = mw.build_workout("Seuil 3x10", THRESHOLD_BLOCKS, ftp_w=200, workout_id=123)
    steps = w["WorkoutStepsArray"]

    assert w["Id"] == 123 and w["Mode"] == "E_Ride" and w["IsIntervals"] is True
    assert w["StepCount"] == len(steps) == 1 + 6 + 1 + 1
    assert w["Time"] == 600 + 3 * 900 + 300 + 300
    assert [s["Id"] for s in steps] == list(range(1, len(steps) + 1))
    warmup = steps[0]
    assert (warmup["StepType"], warmup["StartPower"], warmup["EndPower"]) == ("E_WarmUp", 0.5, 0.75)
    assert warmup["WorkoutMessage"] == [{"Id": 1, "Time": 0, "Message": "On s'échauffe"}]
    on, off = steps[1], steps[2]
    assert (on["Power"], on["Time"], on["Rpm"], on["IntervalId"]) == (0.95, 600, 90, 1)
    assert (off["Power"], off["Time"], off["IntervalId"]) == (0.55, 300, 1)
    assert steps[-1]["StepType"] == "E_CoolDown"
    assert 0.8 < w["IF"] < 0.9 and w["TSS"] > 0 and w["KJ"] > 0


@pytest.mark.parametrize(
    "blocks, fragment",
    [
        ([], "au moins un bloc"),
        ([{"type": "sprint", "duration_s": 10}], "type inconnu"),
        ([{"type": "steady", "duration_s": 600}], "power_pct"),
        ([{"type": "steady", "duration_s": 600, "power_pct": 400}], "entre 0 et 300"),
        ([{"type": "steady", "duration_s": 60, "power_pct": 80, "messages": [{"at_s": 90, "text": "x"}]}], "hors de l'étape"),
    ],
)
def test_build_workout_rejects_invalid_blocks(blocks: list, fragment: str) -> None:
    with pytest.raises(mw.WorkoutError, match=fragment):
        mw.build_workout("Test", blocks)


async def test_push_workout_dry_run_by_default(garmin: MagicMock, mywhoosh: MagicMock) -> None:
    result = await build_mywhoosh_tools(garmin, mywhoosh)["mywhoosh_push_workout"]("Seuil 3x10", THRESHOLD_BLOCKS)
    assert result["status"] == "dry_run" and result["seance"]["duree_minutes"] == 65
    mywhoosh.upload_custom_workout.assert_not_called()


async def test_push_workout_creates_once(garmin: MagicMock, mywhoosh: MagicMock) -> None:
    tool = build_mywhoosh_tools(garmin, mywhoosh)["mywhoosh_push_workout"]
    mywhoosh.list_custom_workouts.return_value = []
    created = await tool("Seuil 3x10", THRESHOLD_BLOCKS, dry_run=False)
    assert created["status"] == "created"
    sent = mywhoosh.upload_custom_workout.call_args.args[0]
    assert sent["Name"] == "Seuil 3x10" and sent["StepCount"] == 9

    mywhoosh.list_custom_workouts.return_value = [{"Name": "Seuil 3x10"}]
    again = await tool("Seuil 3x10", THRESHOLD_BLOCKS, dry_run=False)
    assert again["status"] == "already_present"
    assert mywhoosh.upload_custom_workout.call_count == 1


async def test_push_workout_invalid_input_and_api_error(garmin: MagicMock, mywhoosh: MagicMock) -> None:
    tool = build_mywhoosh_tools(garmin, mywhoosh)["mywhoosh_push_workout"]
    bad = await tool("Test", [{"type": "steady"}], dry_run=False)
    assert bad["error_type"] == "invalid_input"
    mywhoosh.list_custom_workouts.return_value = []
    mywhoosh.upload_custom_workout.side_effect = MyWhooshError("séance refusée par MyWhoosh : quota", "unknown")
    failed = await tool("Test", THRESHOLD_BLOCKS, dry_run=False)
    assert failed["status"] == "error" and "quota" in failed["message"]


def test_user_id_read_from_jwt() -> None:
    import base64, json as _json

    payload = base64.urlsafe_b64encode(_json.dumps({"userId": "u-1"}).encode()).decode().rstrip("=")
    client = MyWhooshClient("a@b.c", "pw", device_id="d")
    client._token = f"h.{payload}.s"
    assert client._user_id() == "u-1"


# -- FTP depuis le Ramp Test -----------------------------------------------------------


def test_power_series_and_best_minute() -> None:
    series = fd.power_series(make_ride_fit(seconds=120))  # 150, 151, … 269 W
    assert len(series) == 120 and series[0] == 150 and series[-1] == 269
    assert fd.best_mean_power(series, 60) == pytest.approx(239.5)
    assert fd.best_mean_power(series[:30], 60) is None
    # Trou court comblé, trou long compté à 0 W.
    assert fd.best_mean_power([300] * 10 + [0] * 50, 60) == 50


async def test_get_ftp_from_latest_ramp_test(garmin: MagicMock, mywhoosh: MagicMock) -> None:
    mywhoosh.list_activities.return_value = [
        {"title": "MyWhoosh - Tour de Corse", "activityFileId": "a"},
        {"title": "MyWhoosh - Ramp Test - MyWhoosh", "activityFileId": "b"},
    ]
    mywhoosh.download_fit.return_value = make_ride_fit(seconds=120)

    result = await build_mywhoosh_tools(garmin, mywhoosh)["mywhoosh_get_ftp"]()

    assert result["status"] == "ok"
    assert result["meilleure_minute_w"] == 240 and result["ftp_watts"] == 180
    assert result["test"] == {"nom": "MyWhoosh - Ramp Test - MyWhoosh", "date": "2026-11-17"}
    mywhoosh.download_fit.assert_called_once_with(mywhoosh.list_activities.return_value[1])


async def test_get_ftp_without_ramp_test_is_no_data(garmin: MagicMock, mywhoosh: MagicMock) -> None:
    mywhoosh.list_activities.return_value = [{"title": "MyWhoosh - Tour de Corse"}]
    result = await build_mywhoosh_tools(garmin, mywhoosh)["mywhoosh_get_ftp"]()
    assert result["status"] == "no_data"
    mywhoosh.download_fit.assert_not_called()


# -- ressenti conservé par la fusion ---------------------------------------------------

_FEEDBACK = {"status": "ok", "sensations": "Fort", "sensations_brut": 75, "effort_percu_sur_10": 7, "effort_percu_libelle": "Difficile"}


async def test_merge_reads_feedback_before_delete_and_copies_it(garmin: MagicMock, mywhoosh: MagicMock) -> None:
    mywhoosh.download_fit.return_value = make_ride_fit()
    garmin.download_activity_fit.return_value = make_watch_fit()
    garmin.upload_activity_file.return_value = {"detailedImportResult": {"successes": [{"internalId": 7}]}}
    calls: list[str] = []
    garmin.get_activity_feedback.side_effect = lambda aid: calls.append(f"lire {aid}") or _FEEDBACK
    garmin.set_activity_feedback.side_effect = lambda aid, **kw: calls.append(f"ecrire {aid} {kw}")
    garmin.delete_activity.side_effect = lambda aid: calls.append(f"supprimer {aid}")

    result = await build_mywhoosh_tools(garmin, mywhoosh)["mywhoosh_merge_hr"](garmin_activity_id=42, delete_original=True)

    assert result["ressenti_original"]["effort_percu_sur_10"] == 7
    assert result["copie_ressenti"] == {"statut": "copie"} and result["original_supprime"] is True
    assert calls == ["lire 42", "ecrire 7 {'feel': 75, 'rpe': 70}", "lire 7", "supprimer 42"]


async def test_merge_feedback_not_persisted_or_absent(garmin: MagicMock, mywhoosh: MagicMock) -> None:
    mywhoosh.download_fit.return_value = make_ride_fit()
    garmin.download_activity_fit.return_value = make_watch_fit()
    garmin.upload_activity_file.return_value = {"detailedImportResult": {"successes": [{"internalId": 7}]}}
    tool = build_mywhoosh_tools(garmin, mywhoosh)["mywhoosh_merge_hr"]

    garmin.get_activity_feedback.side_effect = [_FEEDBACK, {"status": "no_data", "sensations_brut": None, "effort_percu_sur_10": None}]
    lost = await tool(garmin_activity_id=42)
    assert lost["copie_ressenti"] == {"statut": "non_persiste"}

    garmin.get_activity_feedback.side_effect = None
    garmin.get_activity_feedback.return_value = {"status": "no_data"}
    garmin.set_activity_feedback.reset_mock()
    none = await tool(garmin_activity_id=42)
    assert none["ressenti_original"]["status"] == "no_data" and none["copie_ressenti"] == {"statut": "rien_a_copier"}
    garmin.set_activity_feedback.assert_not_called()
    garmin.delete_activity.assert_not_called()


async def test_get_ftp_reports_garmin_activity_mean_power_and_duration(garmin: MagicMock, mywhoosh: MagicMock) -> None:
    mywhoosh.list_activities.return_value = [{"title": "MyWhoosh - Ramp Test - MyWhoosh", "activityFileId": "b"}]
    mywhoosh.download_fit.return_value = make_ride_fit(seconds=120)
    garmin.get_activities_by_date.return_value = [_garmin_act(START, 2, "virtual_ride")]

    result = await build_mywhoosh_tools(garmin, mywhoosh)["mywhoosh_get_ftp"]()

    assert result["ftp_watts"] == 180 and result["meilleure_minute_w"] == 240  # champs historiques
    assert result["activity_id"] == 42
    assert result["puissance_moyenne_test_w"] == 210  # moyenne de 150 à 269 W
    assert result["duree_test_s"] == 120
