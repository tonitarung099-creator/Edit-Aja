from __future__ import annotations

import base64
import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .candidates import LocalCandidate
from .core import creation_flags
from .subtitles import SubtitleTrack, format_ms

DEFAULT_MODEL = "gemini-3.5-flash-lite"
API_ROOT = "https://generativelanguage.googleapis.com/v1beta"

# Hemat token: Gemini hanya menerima gambar kecil + SRT, bukan video/audio.
FRAME_OFFSETS_MS = (-3000, -800, 0, 800, 3000)
FRAME_WIDTH = 448
SEMANTIC_ZONE_LIMIT_MS = 6000


@dataclass
class GeminiUsage:
    requests: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class GeminiClient:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL):
        self.api_key = api_key.strip()
        self.model = model.strip() or DEFAULT_MODEL
        self.usage = GeminiUsage()
        if not self.api_key:
            raise ValueError("Gemini API key belum diisi.")

    def _post(self, payload: dict[str, Any], timeout: int = 90) -> dict[str, Any]:
        url = f"{API_ROOT}/models/{self.model}:generateContent"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(body).get("error", {}).get("message") or body
            except Exception:
                detail = body
            raise RuntimeError(f"Gemini API {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Tidak dapat terhubung ke Gemini: {exc.reason}") from exc

        self.usage.requests += 1
        meta = data.get("usageMetadata") or {}
        self.usage.prompt_tokens += int(meta.get("promptTokenCount") or 0)
        self.usage.output_tokens += int(meta.get("candidatesTokenCount") or 0)
        self.usage.total_tokens += int(meta.get("totalTokenCount") or 0)
        return data

    def test_connection(self) -> dict[str, Any]:
        payload = {
            "contents": [{"parts": [{"text": "Return only JSON: {\"ok\":true}"}]}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": 32,
                "responseMimeType": "application/json",
            },
        }
        data = self._post(payload, timeout=30)
        text = _response_text(data)
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = {"raw": text}
        return {
            "ok": True,
            "model": self.model,
            "response": parsed,
            "usage": self.usage.__dict__.copy(),
        }

    def verify_candidates(
        self,
        ffmpeg: str,
        source: Path,
        target_ms: int,
        candidates: list[LocalCandidate],
        subtitles: SubtitleTrack | None,
        frame_width: int = FRAME_WIDTH,
    ) -> dict[str, Any]:
        if not candidates:
            raise ValueError("Tidak ada kandidat untuk diverifikasi Gemini.")

        prompt = _semantic_prompt(target_ms, candidates, subtitles)
        parts: list[dict[str, Any]] = [{"text": prompt}]

        for i, candidate in enumerate(candidates, 1):
            parts.append({
                "text": (
                    f"KANDIDAT {i} · pusat {format_ms(candidate.time_ms)}. "
                    "Urutan gambar berikut bergerak dari sebelum → sesudah kandidat. "
                    "Tidak ada audio yang dikirim."
                )
            })
            for offset_ms in FRAME_OFFSETS_MS:
                frame_ms = max(0, candidate.time_ms + offset_ms)
                jpg = extract_frame_jpeg(ffmpeg, source, frame_ms, frame_width)
                sign = "+" if offset_ms > 0 else ""
                parts.append({
                    "text": (
                        f"Kandidat {i} · {sign}{offset_ms} ms · "
                        f"timestamp {format_ms(frame_ms)}"
                    )
                })
                parts.append({
                    "inline_data": {
                        "mime_type": "image/jpeg",
                        "data": base64.b64encode(jpg).decode("ascii"),
                    },
                    "media_resolution": {"level": "MEDIA_RESOLUTION_LOW"},
                })

        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 900,
                "responseMimeType": "application/json",
            },
        }
        data = self._post(payload)
        text = _response_text(data)
        try:
            result = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Gemini tidak mengembalikan JSON valid.") from exc

        try:
            selected_index = int(result["selected_candidate_index"])
        except Exception as exc:
            raise RuntimeError("Gemini tidak mengembalikan selected_candidate_index.") from exc
        if selected_index < 1 or selected_index > len(candidates):
            raise RuntimeError("Gemini memilih kandidat di luar daftar.")

        selected = candidates[selected_index - 1]
        start_off = _bounded_offset(result.get("boundary_start_offset_ms"), -900)
        end_off = _bounded_offset(result.get("boundary_end_offset_ms"), 900)
        preferred_off = _bounded_offset(result.get("preferred_offset_ms"), 0)
        if start_off > end_off:
            start_off, end_off = end_off, start_off
        preferred_off = max(start_off, min(end_off, preferred_off))

        new_content_off = result.get("new_content_starts_offset_ms")
        try:
            new_content_off = (
                _bounded_offset(new_content_off, None)
                if new_content_off is not None else None
            )
        except Exception:
            new_content_off = None

        result["selected_candidate_index"] = selected_index
        result["candidate_time_ms"] = selected.time_ms
        result["candidate_time"] = format_ms(selected.time_ms)
        result["boundary_start_ms"] = max(0, selected.time_ms + start_off)
        result["boundary_end_ms"] = max(0, selected.time_ms + end_off)
        result["preferred_time_ms"] = max(0, selected.time_ms + preferred_off)
        result["new_content_starts_ms"] = (
            max(0, selected.time_ms + int(new_content_off))
            if new_content_off is not None else None
        )
        result["target_ms"] = target_ms
        result["target"] = format_ms(target_ms)
        result["analysis_mode"] = "frames+srt"
        result["frames_per_candidate"] = len(FRAME_OFFSETS_MS)
        result["usage"] = self.usage.__dict__.copy()
        return result


def _bounded_offset(value: Any, default: int | None) -> int:
    if value is None:
        if default is None:
            raise ValueError("offset kosong")
        value = default
    try:
        offset = int(round(float(value)))
    except Exception:
        if default is None:
            raise
        offset = default
    return max(-SEMANTIC_ZONE_LIMIT_MS, min(SEMANTIC_ZONE_LIMIT_MS, offset))


def extract_frame_jpeg(
    ffmpeg: str,
    source: Path,
    time_ms: int,
    width: int = FRAME_WIDTH,
) -> bytes:
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{max(0, time_ms) / 1000:.3f}",
        "-i", str(source),
        "-frames:v", "1",
        "-vf", f"scale={max(256, int(width))}:-2",
        "-q:v", "6",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "pipe:1",
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creation_flags(),
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        err = proc.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(err.strip() or "Gagal mengambil frame untuk Gemini.")
    return proc.stdout


def _response_text(data: dict[str, Any]) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        feedback = data.get("promptFeedback") or {}
        raise RuntimeError(
            "Gemini tidak menghasilkan kandidat respons: "
            + json.dumps(feedback, ensure_ascii=False)
        )
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(str(p.get("text") or "") for p in parts).strip()
    if not text:
        raise RuntimeError("Respons Gemini kosong.")
    marker = chr(96) * 3
    if text.startswith(marker):
        text = text.strip(chr(96)).strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return text


def _semantic_prompt(
    target_ms: int,
    candidates: list[LocalCandidate],
    subtitles: SubtitleTrack | None,
) -> str:
    rows = []
    for i, candidate in enumerate(candidates, 1):
        rows.append(
            f"{i}. pusat={format_ms(candidate.time_ms)} | "
            f"jarak target={candidate.distance_ms/1000:.1f}s | "
            f"visual_change={candidate.visual} | silence_lokal={candidate.silence} | "
            f"subtitle_gap={candidate.subtitle_gap} | dialogue_edge={candidate.dialogue_edge} | "
            f"subtitle_safe={candidate.subtitle_safe}"
        )
        if subtitles:
            excerpt = subtitles.nearby_text(
                candidate.time_ms,
                radius_ms=12_000,
                max_chars=1800,
            )
            rows.append("SRT sekitar kandidat:\n" + (excerpt or "(tidak ada subtitle)"))

    return f"""
Tugas: tentukan BATAS NARATIF film paling natural di sekitar target
{format_ms(target_ms)}. Target sekitar 15 menit hanyalah referensi durasi part.

INPUT YANG ANDA TERIMA:
- beberapa frame gambar bertimestamp sebelum/sesudah setiap kandidat;
- SRT di sekitar kandidat;
- petunjuk lokal visual, silence, subtitle gap, dan dialogue edge.
TIDAK ADA AUDIO dan TIDAK ADA VIDEO yang dikirim. Jangan mengarang informasi audio
yang tidak dapat dibuktikan dari SRT/data lokal.

ATURAN UTAMA:
- Pergantian gambar/shot/kamera BUKAN otomatis batas part.
- Prioritaskan keutuhan scene, percakapan, aksi, reaksi, dan sebab-akibat.
- Jangan memisahkan pertanyaan dan jawaban yang jelas masih satu percakapan.
- Jangan potong di tengah subtitle/dialog yang tercatat.
- Jika SRT menunjukkan dialog/isi baru mulai sebelum gambar berubah, batas BOLEH
  berada sebelum dialog baru itu.
- Jika gambar berubah tetapi subtitle/kejadian lama jelas masih berlanjut, jangan
  otomatis memotong pada perubahan gambar.
- Silence lokal hanyalah petunjuk, bukan kewajiban.
- Pilih kandidat yang membuat akhir Part N terasa selesai dan Part N+1 terasa wajar.

Anda TIDAK perlu memilih keyframe atau frame encoding. Berikan ZONA BATAS sempit.
MiniCut akan membaca PTS frame asli dan mengunci keputusan Anda ke frame nyata setelahnya.

Semua offset di JSON dalam MILIDETIK relatif terhadap pusat kandidat:
0 = pusat kandidat, negatif = sebelum, positif = sesudah.
Gunakan zona sesempit mungkin yang masih masuk akal. Jangan keluar ±6000 ms.

Kandidat:
{chr(10).join(rows)}

Kembalikan HANYA JSON valid:
{{
  "selected_candidate_index": 1,
  "boundary_start_offset_ms": -500,
  "boundary_end_offset_ms": 400,
  "preferred_offset_ms": -100,
  "new_content_starts_offset_ms": 300,
  "cut_intent": "before_new_dialogue|after_old_dialogue|scene_transition|action_complete|safe_gap|other",
  "confidence": 0.0,
  "dialogue_safe": true,
  "action_safe": true,
  "needs_review": false,
  "reason": "alasan singkat dalam Bahasa Indonesia"
}}

Jika awal konten/dialog baru tidak dapat ditentukan dari SRT/frame, gunakan
new_content_starts_offset_ms=null.
Jika konteks gambar+SRT tidak cukup, set needs_review=true dan confidence lebih rendah.
""".strip()
