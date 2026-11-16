"""Test dell'API di AI Voice Remover.

L'elaborazione Demucs viene sostituita da un finto worker: i test restano
veloci e non richiedono GPU né download di modelli.
"""

from __future__ import annotations

import io
import json
import sys
import time
import wave
from pathlib import Path

import pytest

from avr import create_app
from avr.config import Settings
from avr.jobs import JobStore


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=0,
        device="cpu",
        device_label="CPU",
        device_name="",
        max_upload_mb=10,
        retention_hours=1,
        concurrency=1,
        job_timeout=0,
        uploads_dir=tmp_path / "uploads",
        outputs_dir=tmp_path / "outputs",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        default_model="htdemucs_ft",
        default_quality="turbo",
        default_format="mp3",
        denoise_python=Path(sys.executable),
        denoise_ready=True,
        denoise_max_minutes=15,
    )


def wav_bytes(seconds: float = 0.4, rate: int = 8000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(seconds * rate))
    return buffer.getvalue()


@pytest.fixture()
def app(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    settings.ensure_dirs()

    def fake_execute(self, job):  # noqa: ANN001
        time.sleep(0.35)
        with self._lock:
            if job.get("status") != "processing":
                return
            job_dir = self._job_dir(job["id"])
            job_dir.mkdir(parents=True, exist_ok=True)
            payload = b"ID3-fake-audio"
            if job.get("kind") == "denoise":
                ext = "mp4" if job.get("format") == "video" else job.get("format", "mp3")
                filename = f"clean.{ext}"
                (job_dir / filename).write_bytes(payload)
                output_media = "video" if job.get("format") == "video" else "audio"
                files = {
                    "clean": {
                        "file": filename,
                        "size": len(payload),
                        "key": "clean",
                        "label": "Clip pulita",
                        "media": output_media,
                    }
                }
                peaks = {"clean": [0.2, 0.6, 0.9], "original": [0.3, 0.6, 0.9]} if output_media == "audio" else {}
                manifest = {
                    "kind": "denoise",
                    "model": "DeepFilterNet3",
                    "model_label": "DeepFilterNet3",
                    "strength_db": job.get("strength_db", 8),
                    "format": job.get("format"),
                    "output_media": output_media,
                    "video_reencoded": False,
                    "samplerate": 48000,
                    "duration": 0.4,
                    "files": files,
                    "peaks": peaks,
                    "elapsed": 0.35,
                }
                (job_dir / "result.json").write_text(json.dumps(manifest), encoding="utf-8")
                job.update(
                    status="completed",
                    stage="done",
                    progress=100,
                    message="Pulizia completata",
                    files=files,
                    duration=0.4,
                    result={
                        "kind": "denoise",
                        "duration": 0.4,
                        "samplerate": 48000,
                        "elapsed": 0.35,
                        "model_label": "DeepFilterNet3",
                        "output_media": output_media,
                        "strength_db": job.get("strength_db", 8),
                    },
                    finished_at=time.time(),
                    eta_seconds=0,
                )
                self._save(force=True)
                return

            if job.get("kind") == "polish":
                ext = job.get("format", "mp3")
                filename = f"clean.{ext}"
                (job_dir / filename).write_bytes(payload)
                files = {
                    "clean": {
                        "file": filename,
                        "size": len(payload),
                        "key": "clean",
                        "label": "Stem pulito",
                        "media": "audio",
                    }
                }
                manifest = {
                    "kind": "polish",
                    "mode": job.get("mode", "voice"),
                    "model": job.get("model"),
                    "model_label": "HTDemucs FT · 2° passaggio",
                    "target": job.get("target", "instrumental"),
                    "residual": job.get("residual", 0.6),
                    "hum_mode": job.get("hum_mode", "off"),
                    "hum_hz": 50 if job.get("hum_mode") == "50" else None,
                    "hum_applied": job.get("hum_mode") in {"50", "60", "auto"},
                    "hum_bands": 10,
                    "hum_gain_db": -20,
                    "format": job.get("format"),
                    "output_media": "audio",
                    "samplerate": 44100,
                    "duration": 0.4,
                    "files": files,
                    "peaks": {"clean": [0.2, 0.5, 0.8], "original": [0.3, 0.6, 0.9]},
                    "elapsed": 0.35,
                }
                (job_dir / "result.json").write_text(json.dumps(manifest), encoding="utf-8")
                job.update(
                    status="completed",
                    stage="done",
                    progress=100,
                    message="Pulizia residui completata",
                    files=files,
                    duration=0.4,
                    result={
                        "kind": "polish",
                        "mode": manifest["mode"],
                        "duration": 0.4,
                        "samplerate": 44100,
                        "elapsed": 0.35,
                        "model_label": manifest["model_label"],
                        "output_media": "audio",
                        "target": manifest["target"],
                        "residual": manifest["residual"],
                        "hum_mode": manifest["hum_mode"],
                        "hum_hz": manifest["hum_hz"],
                        "hum_applied": manifest["hum_applied"],
                    },
                    finished_at=time.time(),
                    eta_seconds=0,
                )
                self._save(force=True)
                return

            files = {}
            for key in ("instrumental", "vocals", "drums", "bass", "other"):
                (job_dir / f"{key}.mp3").write_bytes(payload)
                files[key] = {
                    "file": f"{key}.mp3",
                    "size": len(payload),
                    "key": key,
                    "label": key.title(),
                }
            peaks = {"instrumental": [0.1, 0.5, 1.0], "vocals": [0.2, 0.4, 0.8], "original": [0.3, 0.6, 0.9]}
            manifest = {
                "model": job["model"],
                "model_label": "HTDemo",
                "quality": job["quality"],
                "format": job["format"],
                "samplerate": 44100,
                "duration": 0.4,
                "sources": ["drums", "bass", "other", "vocals"],
                "files": files,
                "peaks": peaks,
                "elapsed": 0.35,
                "device": "cpu",
            }
            (job_dir / "result.json").write_text(json.dumps(manifest), encoding="utf-8")
            job.update(
                status="completed",
                stage="done",
                progress=100,
                message="Separazione completata",
                files=files,
                duration=0.4,
                result={"duration": 0.4, "samplerate": 44100, "elapsed": 0.35},
                finished_at=time.time(),
                eta_seconds=0,
            )
            self._save(force=True)

    monkeypatch.setattr(JobStore, "_execute", fake_execute)

    application = create_app(settings)
    application.config.update(TESTING=True)
    yield application
    application.extensions["avr"]["store"].shutdown()


@pytest.fixture()
def client(app):
    return app.test_client()


def upload(client, name="test.wav", **options):
    data = {
        "file": (io.BytesIO(wav_bytes()), name),
        "model": options.get("model", "htdemucs_ft"),
        "quality": options.get("quality", "turbo"),
        "format": options.get("format", "mp3"),
    }
    return client.post("/api/upload", data=data, content_type="multipart/form-data")


def wait_for(client, job_id, statuses=("completed", "error"), timeout=8.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        last = response.get_json()
        if last["status"] in statuses:
            return last
        time.sleep(0.1)
    raise AssertionError(f"Job {job_id} non terminato, ultimo stato: {last}")


def test_index_and_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "AI Voice Remover" in body
    assert "Confronta originale" in body
    csp = response.headers.get("Content-Security-Policy")
    assert csp and "default-src 'self'" in csp
    assert response.headers.get("X-Content-Type-Options") == "nosniff"


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.get_json()
    assert data["app"] == "AI Voice Remover"
    assert data["ok"] is True
    assert data["max_upload_mb"] == 10
    assert "models_cached" in data
    assert data["denoise"]["available"] is True
    assert data["denoise"]["engine"] == "DeepFilterNet3"


def test_upload_rejects_invalid_format(client):
    response = upload(client, name="not-audio.txt")
    assert response.status_code == 400
    assert "Formato non supportato" in response.get_json()["error"]


def test_upload_rejects_invalid_options(client):
    response = upload(client, model="modello-inesistente")
    assert response.status_code == 400


def test_upload_requires_file(client):
    response = client.post("/api/upload", data={}, content_type="multipart/form-data")
    assert response.status_code == 400


def test_full_job_lifecycle(client):
    response = upload(client, name="Brano Test.wav")
    assert response.status_code == 202
    job = response.get_json()["job"]
    job_id = job["id"]
    assert job["name"] == "Brano Test.wav"
    assert job["duration"] == pytest.approx(0.4, abs=0.1)

    completed = wait_for(client, job_id)
    assert completed["status"] == "completed"
    assert completed["progress"] == 100
    assert set(completed["files"]) == {"instrumental", "vocals", "drums", "bass", "other"}
    assert completed["files"]["instrumental"]["name"].endswith("_strumentale.mp3")
    assert completed["has_source"] is True

    listing = client.get("/api/jobs").get_json()
    assert any(item["id"] == job_id for item in listing["jobs"])

    audio = client.get(f"/api/jobs/{job_id}/files/instrumental")
    assert audio.status_code == 200
    assert audio.data.startswith(b"ID3")

    download = client.get(f"/api/jobs/{job_id}/files/vocals?download=1")
    assert download.status_code == 200
    assert "attachment" in download.headers.get("Content-Disposition", "")

    source = client.get(f"/api/jobs/{job_id}/source")
    assert source.status_code == 200
    assert source.mimetype in {"audio/wav", "audio/x-wav"}

    peaks = client.get(f"/api/jobs/{job_id}/peaks/instrumental")
    assert peaks.status_code == 200
    assert peaks.get_json()["peaks"] == [0.1, 0.5, 1.0]

    bundle = client.get(f"/api/jobs/{job_id}/bundle.zip")
    assert bundle.status_code == 200
    assert bundle.mimetype == "application/zip"
    assert len(bundle.data) > 100

    deleted = client.delete(f"/api/jobs/{job_id}")
    assert deleted.status_code == 200
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    assert client.get(f"/api/jobs/{job_id}/files/instrumental").status_code == 404


def test_cancel_retry_and_delete(client):
    response = upload(client, name="cancel.wav")
    job_id = response.get_json()["job"]["id"]

    cancelled = client.post(f"/api/jobs/{job_id}/cancel").get_json()
    assert cancelled["status"] == "cancelled"

    retried = client.post(f"/api/jobs/{job_id}/retry", json={})
    assert retried.status_code == 202
    assert retried.get_json()["status"] in {"queued", "processing"}

    completed = wait_for(client, job_id)
    assert completed["status"] == "completed"

    assert client.delete(f"/api/jobs/{job_id}").status_code == 200


def test_unknown_job_returns_404(client):
    assert client.get("/api/jobs/abcdef1234").status_code == 404
    assert client.post("/api/jobs/abcdef1234/cancel").status_code == 404
    assert client.delete("/api/jobs/abcdef1234").status_code == 404


def test_denoise_audio_job(client):
    response = client.post(
        "/api/upload",
        data={
            "file": (io.BytesIO(wav_bytes()), "intervista.wav"),
            "kind": "denoise",
            "strength": "natural",
            "format": "mp3",
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 202
    job = response.get_json()["job"]
    assert job["kind"] == "denoise"
    assert job["status"] == "queued"
    assert job["quality_label"] == "Naturale"
    assert job["model_label"] == "DeepFilterNet3"

    completed = wait_for(client, job["id"])
    assert completed["status"] == "completed"
    assert completed["result"]["output_media"] == "audio"
    assert set(completed["files"]) == {"clean"}
    assert completed["files"]["clean"]["name"].endswith("_pulito.mp3")

    audio = client.get(f"/api/jobs/{job['id']}/files/clean")
    assert audio.status_code == 200
    assert "attachment" not in audio.headers.get("Content-Disposition", "")

    peaks = client.get(f"/api/jobs/{job['id']}/peaks/clean")
    assert peaks.status_code == 200
    assert peaks.get_json()["peaks"] == [0.2, 0.6, 0.9]


def test_denoise_rejects_invalid_options(client):
    data = {
        "file": (io.BytesIO(wav_bytes()), "voce.wav"),
        "kind": "denoise",
        "strength": "inesistente",
        "format": "mp3",
    }
    assert client.post("/api/upload", data=data, content_type="multipart/form-data").status_code == 400

    data["strength"] = "natural"
    data["file"] = (io.BytesIO(wav_bytes()), "voce.wav")
    data["format"] = "video"
    response = client.post("/api/upload", data=data, content_type="multipart/form-data")
    assert response.status_code == 400
    assert "clip video" in response.get_json()["error"]


def test_denoise_unavailable_returns_503(client, app):
    settings = app.config["AVR_SETTINGS"]
    object.__setattr__(settings, "denoise_ready", False)
    data = {
        "file": (io.BytesIO(wav_bytes()), "voce.wav"),
        "kind": "denoise",
        "strength": "natural",
        "format": "mp3",
    }
    response = client.post("/api/upload", data=data, content_type="multipart/form-data")
    assert response.status_code == 503
    object.__setattr__(settings, "denoise_ready", True)


def test_polish_separation_stem(client):
    response = upload(client, name="Brano Mix.wav")
    source_id = response.get_json()["job"]["id"]
    source = wait_for(client, source_id)
    assert source["status"] == "completed"

    polished = client.post(
        f"/api/jobs/{source_id}/polish",
        json={"target": "instrumental", "strength": "medium", "format": "mp3"},
    )
    assert polished.status_code == 202
    job = polished.get_json()["job"]
    assert job["kind"] == "polish"
    assert job["source_job"] == source_id
    assert job["quality_label"] == "Equilibrato · 60% + ronzio Auto"
    assert job["model_label"].endswith("2° passaggio")

    completed = wait_for(client, job["id"])
    assert completed["status"] == "completed"
    assert set(completed["files"]) == {"clean"}
    assert completed["files"]["clean"]["name"].endswith("_pulito.mp3")
    assert completed["result"]["residual"] == 0.6
    assert completed["result"]["target"] == "instrumental"

    peaks = client.get(f"/api/jobs/{job['id']}/peaks/clean")
    assert peaks.status_code == 200
    assert len(peaks.get_json()["peaks"]) == 3

    source_file = client.get(f"/api/jobs/{job['id']}/source")
    assert source_file.status_code == 200

    assert client.delete(f"/api/jobs/{job['id']}").status_code == 200
    assert client.delete(f"/api/jobs/{source_id}").status_code == 200


def test_polish_requires_completed_separation(client):
    response = upload(client, name="Non pronto.wav")
    job_id = response.get_json()["job"]["id"]
    polished = client.post(f"/api/jobs/{job_id}/polish", json={})
    assert polished.status_code in {409, 400}


def test_polish_hum_only(client):
    response = upload(client, name="Ronzio.wav")
    source_id = response.get_json()["job"]["id"]
    wait_for(client, source_id)

    invalid = client.post(
        f"/api/jobs/{source_id}/polish",
        json={"mode": "hum", "hum": "off"},
    )
    assert invalid.status_code == 400

    response = client.post(
        f"/api/jobs/{source_id}/polish",
        json={"mode": "hum", "hum": "50", "format": "wav"},
    )
    assert response.status_code == 202
    job = response.get_json()["job"]
    assert job["mode"] == "hum"
    assert job["quality_label"] == "Solo ronzio · 50 Hz"

    completed = wait_for(client, job["id"])
    assert completed["status"] == "completed"
    assert completed["result"]["hum_applied"] is True
    assert completed["result"]["hum_hz"] == 50


