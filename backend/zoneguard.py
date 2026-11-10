"""BlueShield ZoneGuard v2: resumable background sync + global MPA screening.

MPA presence is a geographic flag, NOT proof that fishing is prohibited or illegal.
Requires Shapely. Uses UNEP-WCMC/IUCN WDPA Marine and Coastal FeatureServer.
"""
from __future__ import annotations
import json, threading, time
from numbers import Integral
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from typing import Any

try:
    from shapely.geometry import shape, Point
    from shapely.strtree import STRtree
except Exception:
    shape = Point = STRtree = None

SERVICE = "https://data-gis.unep-wcmc.org/server/rest/services/ProtectedSites/WDPA_Marine_and_Coastal/FeatureServer/1"
DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_FILE = DATA_DIR / "marine_protected_areas.geojson"
PARTIAL_FILE = DATA_DIR / "marine_protected_areas.download.ndjson"
PROGRESS_FILE = DATA_DIR / "marine_protected_areas.progress.json"
ZONE_INDEX_FILE = DATA_DIR / "marine_protected_areas.index.ndjson"
ATTRIBUTION = "UNEP-WCMC and IUCN (2026), Protected Planet: WDPCA, November 2026."
BATCH_SIZE = 250
MAX_RETRIES = 4

_cache: dict[str, Any] = {"loaded": False, "features": [], "geometries": [], "tree": None, "last_error": None}
_job_lock = threading.Lock()
_index_lock = threading.Lock()
_index_cache: list[dict] | None = None
_index_state: dict[str, Any] = {"state": "not_built", "last_error": None, "rows": 0, "updated_at": None}
_spatial_load_lock = threading.Lock()
_spatial_load_state: dict[str, Any] = {"state": "not_loaded", "last_error": None}
_job: dict[str, Any] = {"state": "idle", "total_ids": 0, "completed_ids": 0, "features_saved": 0,
                        "progress_percent": 0.0, "batch_size": BATCH_SIZE, "last_error": None,
                        "started_at": None, "updated_at": None}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _request_json(url: str, timeout: int = 120) -> dict:
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            req = Request(url, headers={"User-Agent": "BlueShieldGlobal/2.0 (ZoneGuard data sync)"})
            with urlopen(req, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(f"ArcGIS error: {data['error']}")
            return data
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError, RuntimeError) as exc:
            last = exc
            if attempt + 1 < MAX_RETRIES:
                time.sleep(min(2 ** attempt, 12))
    raise RuntimeError(f"UNEP-WCMC request failed after {MAX_RETRIES} attempts: {last}")


def _load_file() -> dict:
    if not DATA_FILE.exists():
        return {"type": "FeatureCollection", "features": []}
    with DATA_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def _activate(collection: dict) -> None:
    features, geometries = [], []
    for feature in collection.get("features") or []:
        geom = feature.get("geometry")
        if not geom or not shape:
            continue
        try:
            g = shape(geom)
            if not g.is_empty and g.is_valid:
                features.append(feature); geometries.append(g)
        except Exception:
            continue
    tree = STRtree(geometries) if STRtree and geometries else None
    _cache.update({"loaded": True, "features": features, "geometries": geometries,
                   "tree": tree, "metadata": collection.get("metadata") or {},
                   "last_error": None})


def load_zones() -> dict:
    if not _cache["loaded"]:
        _activate(_load_file())
    return _cache


def _write_final_geojson(feature_count: int, total_ids: int) -> None:
    """Convert checkpoint NDJSON to a GeoJSON file using an atomic replace."""
    tmp = DATA_FILE.with_suffix(".geojson.tmp")
    metadata = {"source": SERVICE, "attribution": ATTRIBUTION, "synced_at": _now(),
                "service_feature_ids": total_ids, "features_downloaded": feature_count}
    with tmp.open("w", encoding="utf-8") as out:
        out.write('{"type":"FeatureCollection","name":"UNEP_WCMC_IUCN_WDPCA_Marine_Coastal_PA","metadata":')
        json.dump(metadata, out, ensure_ascii=False, separators=(",", ":"))
        out.write(',"features":[')
        first = True
        if PARTIAL_FILE.exists():
            with PARTIAL_FILE.open("r", encoding="utf-8") as src:
                for line in src:
                    line = line.strip()
                    if not line:
                        continue
                    if not first:
                        out.write(',')
                    out.write(line)
                    first = False
        out.write(']}')
    tmp.replace(DATA_FILE)


def _run_sync() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _job.update({"state": "running", "started_at": _now(), "updated_at": _now(), "last_error": None,
                     "batch_size": BATCH_SIZE})
        ids_data = _request_json(SERVICE + "/query?" + urlencode({
            "where": "1=1", "returnIdsOnly": "true", "returnGeometry": "false", "f": "json"}), timeout=120)
        ids = ids_data.get("objectIds") or []
        if not ids:
            raise RuntimeError("UNEP-WCMC returned no object IDs.")
        ids = sorted(ids)
        _job["total_ids"] = len(ids)

        # Resume from valid completed object IDs recorded in the checkpoint.
        completed: set[int] = set()
        saved_count = 0
        if PROGRESS_FILE.exists() and PARTIAL_FILE.exists():
            try:
                progress = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
                if progress.get("source") == SERVICE:
                    completed = {int(x) for x in progress.get("completed_object_ids", [])}
                    saved_count = int(progress.get("features_saved", 0))
            except Exception:
                completed, saved_count = set(), 0
        if not completed and PARTIAL_FILE.exists():
            # Do not trust an orphaned partial file without its matching checkpoint.
            PARTIAL_FILE.unlink(missing_ok=True)
        remaining = [oid for oid in ids if int(oid) not in completed]
        _job.update({"completed_ids": len(completed), "features_saved": saved_count,
                     "progress_percent": round(100 * len(completed) / len(ids), 2)})

        for start in range(0, len(remaining), BATCH_SIZE):
            batch = remaining[start:start + BATCH_SIZE]
            payload = _request_json(SERVICE + "/query?" + urlencode({
                "objectIds": ",".join(map(str, batch)), "outFields": "*",
                "returnGeometry": "true", "outSR": "4326", "f": "geojson"}), timeout=180)
            features = payload.get("features") or []
            if not features:
                raise RuntimeError(f"Batch beginning with object ID {batch[0]} returned zero features.")
            # Persist every successful batch before moving on.
            with PARTIAL_FILE.open("a", encoding="utf-8") as out:
                for feature in features:
                    out.write(json.dumps(feature, ensure_ascii=False, separators=(",", ":")) + "\n")
            completed.update(int(x) for x in batch)
            saved_count += len(features)
            PROGRESS_FILE.write_text(json.dumps({"source": SERVICE, "completed_object_ids": sorted(completed),
                                                  "features_saved": saved_count, "updated_at": _now()}), encoding="utf-8")
            _job.update({"completed_ids": len(completed), "features_saved": saved_count,
                         "progress_percent": round(100 * len(completed) / len(ids), 2), "updated_at": _now()})

        _write_final_geojson(saved_count, len(ids))
        # Keep sync completion independent of loading the multi-GB polygon file.
        # Spatial geometries are loaded lazily only when an assessment is requested.
        _cache.update({"loaded": False, "features": [], "geometries": [], "tree": None,
                       "metadata": {"source": SERVICE, "attribution": ATTRIBUTION, "synced_at": _now()},
                       "last_error": None})
        PARTIAL_FILE.unlink(missing_ok=True)
        PROGRESS_FILE.unlink(missing_ok=True)
        _job.update({"state": "completed", "completed_ids": len(ids), "features_saved": saved_count,
                     "progress_percent": 100.0, "updated_at": _now(), "last_error": None})
    except Exception as exc:
        _job.update({"state": "failed", "updated_at": _now(), "last_error": str(exc)})
        _cache["last_error"] = str(exc)
    finally:
        _job_lock.release()


def sync_global_zones() -> dict:
    """Start/resume global sync in a daemon thread and return immediately."""
    if not shape:
        raise RuntimeError("ZoneGuard requires Shapely. Install it with: pip install shapely")
    if not _job_lock.acquire(blocking=False):
        return status()
    _job.update({"state": "starting", "last_error": None, "updated_at": _now()})
    thread = threading.Thread(target=_run_sync, name="ZoneGuard-MPA-Sync", daemon=True)
    thread.start()
    result = status()
    result["sync_message"] = "Global MPA sync started in the background. Poll /api/zoneguard/status for progress."
    return result


def status() -> dict:
    """Return lightweight status without parsing/building the global polygon index.

    Loading a large GeoJSON file and constructing its STRtree can be expensive.
    Status polling must never trigger that work on a FastAPI request thread.
    The polygon count and sync timestamp are reported once the dataset is loaded
    in memory; data_file_exists still reports whether a local dataset is present.
    """
    loaded = bool(_cache.get("loaded"))
    return {"engine": "BlueShield ZoneGuard v2",
            "ready": loaded and bool(_cache.get("tree")),
            "geospatial_dependency": "shapely" if shape else "missing",
            "marine_protected_area_polygons": len(_cache.get("features", [])) if loaded else None,
            "dataset_synced_at": _cache.get("metadata", {}).get("synced_at") if loaded else None,
            "dataset_source": SERVICE,
            "attribution": ATTRIBUTION,
            "legal_interpretation": "MPA presence is a geographic flag, not proof that fishing is prohibited or that a violation occurred.",
            "data_file_exists": DATA_FILE.exists(),
            "zone_list_index": dict(_index_state),
            "spatial_index": dict(_spatial_load_state),
            "last_error": _cache.get("last_error") or _job.get("last_error"),
            "sync": dict(_job)}


def _zone_row(properties: dict) -> dict:
    p = properties or {}
    name = p.get("name_eng") or p.get("name") or "Unnamed protected area"
    return {"id": p.get("site_id") or p.get("site_pid") or p.get("objectid"),
            "name": name, "country": p.get("iso3") or p.get("prnt_iso3"),
            "designation": p.get("desig_eng") or p.get("desig"),
            "iucn_category": p.get("iucn_cat"), "status": p.get("status"), "marine": True}


def _build_zone_list_index() -> None:
    """Stream feature properties from the huge GeoJSON without retaining geometries."""
    global _index_cache
    tmp = ZONE_INDEX_FILE.with_suffix(".ndjson.tmp")
    decoder = json.JSONDecoder()
    marker = '"features"'
    try:
        if not DATA_FILE.exists():
            raise RuntimeError("Marine protected-area GeoJSON file is missing.")

        with DATA_FILE.open("r", encoding="utf-8") as source, tmp.open("w", encoding="utf-8") as output:
            buffer = ""
            eof = False

            # Find the features array without reading the entire file into memory.
            while marker not in buffer:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    raise RuntimeError("Could not find the GeoJSON features array.")
                buffer += chunk

            marker_pos = buffer.index(marker) + len(marker)
            buffer = buffer[marker_pos:]

            # Move to the opening '['.
            while "[" not in buffer:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    raise RuntimeError("GeoJSON features array is incomplete.")
                buffer += chunk
            buffer = buffer[buffer.index("[") + 1:]

            count = 0
            while True:
                buffer = buffer.lstrip()
                while buffer.startswith(","):
                    buffer = buffer[1:].lstrip()

                if buffer.startswith("]"):
                    break

                try:
                    feature, consumed = decoder.raw_decode(buffer)
                except json.JSONDecodeError:
                    if eof:
                        raise RuntimeError("Could not decode a feature while building the zone list index.")
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        eof = True
                    else:
                        buffer += chunk
                    continue

                properties = feature.get("properties") or {}
                output.write(json.dumps(_zone_row(properties), ensure_ascii=False, separators=(",", ":")) + "\n")
                count += 1
                buffer = buffer[consumed:]

                if len(buffer) < 1024 * 1024 and not eof:
                    chunk = source.read(1024 * 1024)
                    if chunk:
                        buffer += chunk
                    else:
                        eof = True

        tmp.replace(ZONE_INDEX_FILE)

        rows = []
        with ZONE_INDEX_FILE.open("r", encoding="utf-8") as index_file:
            for line in index_file:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))

        _index_cache = rows
        _index_state.update({"state": "ready", "last_error": None,
                             "rows": len(rows), "updated_at": _now()})
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        _index_state.update({"state": "failed", "last_error": str(exc), "updated_at": _now()})
    finally:
        _index_lock.release()


def _ensure_zone_list_index() -> bool:
    """Load an existing compact index, or start its one-time background build."""
    global _index_cache

    if _index_cache is not None:
        return True

    if ZONE_INDEX_FILE.exists():
        try:
            rows = []
            with ZONE_INDEX_FILE.open("r", encoding="utf-8") as index_file:
                for line in index_file:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
            with _index_lock:
                _index_cache = rows
                _index_state.update({"state": "ready", "last_error": None,
                                     "rows": len(rows), "updated_at": _now()})
            return True
        except Exception as exc:
            with _index_lock:
                _index_state.update({"state": "failed", "last_error": str(exc), "updated_at": _now()})

    if _index_lock.acquire(blocking=False):
        _index_state.update({"state": "building", "last_error": None, "updated_at": _now()})
        thread = threading.Thread(target=_build_zone_list_index, name="ZoneGuard-ZoneList-Index", daemon=True)
        thread.start()
    return False


def list_zones(limit: int = 100, offset: int = 0, search: str | None = None) -> dict:
    """List MPA metadata without loading polygon geometries or building the STRtree."""
    if not _ensure_zone_list_index():
        return {"total": 0, "offset": offset, "limit": limit, "zones": [],
                "index_state": _index_state.get("state", "building"),
                "message": "The lightweight zone list is being prepared in the background. Poll /api/zoneguard/status and retry this endpoint when zone_list_index.state is ready."}

    rows = _index_cache or []
    if search:
        needle = search.casefold()
        filtered = [row for row in rows if needle in str(row.get("name", "")).casefold()]
    else:
        filtered = rows

    return {"total": len(filtered), "offset": offset, "limit": limit,
            "zones": filtered[offset:offset + limit], "index_state": "ready"}


def _load_spatial_index_background() -> None:
    try:
        _spatial_load_state.update({"state": "loading", "last_error": None, "updated_at": _now()})
        collection = _load_file()
        _activate(collection)
        _spatial_load_state.update({"state": "ready" if _cache.get("tree") else "empty",
                                    "last_error": None, "updated_at": _now()})
    except Exception as exc:
        _spatial_load_state.update({"state": "failed", "last_error": str(exc), "updated_at": _now()})
    finally:
        _spatial_load_lock.release()


def check_position(latitude: float, longitude: float) -> list[dict]:
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return []

    if not _cache.get("loaded"):
        if _spatial_load_lock.acquire(blocking=False):
            _spatial_load_state.update({"state": "starting", "last_error": None, "updated_at": _now()})
            thread = threading.Thread(target=_load_spatial_index_background,
                                      name="ZoneGuard-Spatial-Index", daemon=True)
            thread.start()
        return []

    cache = _cache
    if not cache["tree"]:
        return []
    point = Point(longitude, latitude)
    matches = []
    for candidate in cache["tree"].query(point):
        if isinstance(candidate, Integral) or getattr(candidate, "dtype", None) is not None:
            idx = int(candidate); geom = cache["geometries"][idx]
        else:
            geom = candidate
            try: idx = cache["geometries"].index(geom)
            except ValueError: continue
        if geom.covers(point):
            p = cache["features"][idx].get("properties") or {}
            matches.append({"id": p.get("site_id") or p.get("site_pid") or p.get("objectid"),
                            "name": p.get("name_eng") or p.get("name") or "Unnamed protected area",
                            "country": p.get("iso3") or p.get("prnt_iso3"),
                            "designation": p.get("desig_eng") or p.get("desig"),
                            "iucn_category": p.get("iucn_cat"), "status": p.get("status"),
                            "assessment": "INSIDE_MARINE_PROTECTED_AREA",
                            "fishing_legality": "UNKNOWN_RULES_NOT_LOADED"})
    return matches


def assess_vessels(records: list[dict]) -> dict:
    checked, inside, results = 0, 0, []
    for row in records:
        try:
            lat, lon = float(row.get("latitude")), float(row.get("longitude"))
        except (TypeError, ValueError):
            continue
        checked += 1
        zones = check_position(lat, lon)
        if zones:
            inside += 1
            results.append({"mmsi": str(row.get("mmsi", "")), "name": row.get("name"),
                            "latitude": lat, "longitude": lon, "speed_knots": row.get("speed_knots"),
                            "position_timestamp": row.get("position_timestamp"), "zones": zones,
                            "classification": "PROTECTED_AREA_PRESENCE_REVIEW"})
    return {"engine": "BlueShield ZoneGuard v2", "positions_checked": checked,
            "vessels_inside_mpa": inside, "alerts": results,
            "spatial_index_state": _spatial_load_state.get("state", "not_loaded"),
            "assessment_complete": _spatial_load_state.get("state") == "ready",
            "warning": "If the spatial index is loading, this response is preliminary and must be repeated after spatial_index_state becomes ready. A position inside an MPA does not by itself establish fishing activity or a violation. Check zone-specific legal rules, permitted activities, vessel authorisation, and position uncertainty."}
