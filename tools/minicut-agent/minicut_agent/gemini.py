from __future__ import annotations

import base64
import json
import subprocess
import tempfile
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
SEMANTIC_CLIP_RADIUS_MS = 14_000


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

    def _post(self, payload: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
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
        clip_radius_ms: int = SEMANTIC_CLIP_RADIUS_MS,
    ) -> dict[str, Any]:
        if not candidates:
            raise ValueError("Tidak ada kandidat untuk diverifikasi Gemini.")

        prompt = _semantic_prompt(target_ms, candidates, subtitles, clip_radius_ms)
        parts: list[dict[str, Any]] = [{"text": prompt}]

        for i, candidate in enumerate(candidates, 1):
            clip = extract_context_clip_mp4(
                ffmpeg,
                source,
                candidate.time_ms,
                radius_ms=clip_radius_ms,
            )
            parts.append({
                "text": (
                    f"KANDIDAT {i} · pusat kandidat {format_ms(candidate.time_ms)} · "
                    f"cuplikan sekitar {clip_radius_ms / 1000:.0f} detik sebelum dan sesudah. "
                    "Dengarkan AUDIO dan lihat VISUAL."
                )
            })
            parts.append({
                "inline_data": {
                    "mime_type": "video/mp4",
                    "data": base64.b64encode(clip).decode("ascii"),
                },
                "media_resolution": {"level": "MEDIA_RESOLUTION_LOW"},
            })

        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 1000,
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
        start_off = _bounded_offset(result.get("boundary_start_offset_ms"), -2500)
        end_off = _bounded_offset(result.get("boundary_end_offset_ms"), 2500)
        preferred_off = _bounded_offset(result.get("preferred_offset_ms"), 0)
        if start_off > end_off:
            start_off, end_off = end_off, start_off
        preferred_off = max(start_off, min(end_off, preferred_off))

        new_content_off = result.get("new_content_starts_offset_ms")
        try:
            new_content_off = _bounded_offset(new_content_off, None) if new_content_off is not None else None
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
    return max(-SEMANTIC_CLIP_RADIUS_MS, min(SEMANTIC_CLIP_RADIUS_MS, offset))


def extract_context_clip_mp4(
    ffmpeg: str,
    source: Path,
    center_ms: int,
    radius_ms: int = SEMANTIC_CLIP_RADIUS_MS,
    width: int = 480,
) -> bytes:
    radius_ms = max(4000, min(int(radius_ms), 20_000))
    start_ms = max(0, int(center_ms) - radius_ms)
    duration_ms = radius_ms * 2
    with tempfile.TemporaryDirectory(prefix="minicut-semantic-") as td:
        out = Path(td) / "context.mp4"
        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel", "error",
            "-ss", f"{start_ms / 1000:.3f}",
            "-i", str(source),
            "-t", f"{duration_ms / 1000:.3f}",
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-vf", f"scale={max(320, int(width))}:-2,fps=12",
            "-c:v", "mpeg4",
            "-q:v", "8",
            "-c:a", "aac",
            "-b:a", "64k",
            "-ac", "1",
            "-ar", "24000",
            "-movflags", "+faststart",
            str(out),
        ]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creation_flags(),
            check=False,
        )
        if proc.returncode != 0 or not out.is_file():
            err = proc.stderr.decode("utf-8", errors="replace")
            raise RuntimeError(err.strip() or "Gagal membuat cuplikan semantic untuk Gemini.")
        data = out.read_bytes()
        if not data:
            raise RuntimeError("Cuplikan semantic kosong.")
        return data


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
    clip_radius_ms: int,
) -> str:
    rows = []
    for i, candidate in enumerate(candidates, 1):
        rows.append(
            f"{i}. pusat={format_ms(candidate.time_ms)} | "
            f"jarak target={candidate.distance_ms/1000:.1f}s | "
            f"visual_change={candidate.visual} | silence={candidate.silence} | "
            f"subtitle_gap={candidate.subtitle_gap} | dialogue_edge={candidate.dialogue_edge} | "
            f"subtitle_safe={candidate.subtitle_safe}"
        )
        if subtitles:
            excerpt = subtitles.nearby_text(
                candidate.time_ms, radius_ms=clip_radius_ms + 4000, max_chars=2200
            )
            rows.append("SRT sekitar kandidat:\n" + (excerpt or "(tidak ada subtitle)"))

    return f"""
Tugas Anda adalah menentukan BATAS NARATIF film yang paling natural di sekitar patokan
{format_ms(target_ms)}. Patokan sekitar 15 menit hanyalah referensi durasi part.

JANGAN menganggap pergantian gambar, shot, angle kamera, atau keyframe sebagai titik potong wajib.
Yang menentukan adalah kapan rangkaian cerita lama benar-benar selesai dan bagian berikutnya
secara natural mulai.

Anda mendapat beberapa cuplikan VIDEO DENGAN AUDIO. Untuk tiap kandidat:
- dengarkan kapan dialog lama benar-benar selesai;
- dengarkan apakah dialog/ambience scene baru masuk SEBELUM gambar berganti (J-cut);
- cek apakah suara/dialog scene lama masih berlanjut SETELAH gambar berganti (L-cut);
- jangan pisahkan pertanyaan dan jawaban;
- jangan potong aksi, reaksi, sebab-akibat, atau kejadian yang masih satu rangkaian;
- jangan potong di tengah kata/kalimat/subtitle;
- pergantian lokasi/waktu/kejadian dapat menjadi petunjuk, bukan aturan mutlak;
- pilih batas yang membuat akhir Part N terasa selesai dan awal Part N+1 terasa wajar.

Jika dialog scene baru masuk lebih dulu daripada perubahan visual dan itu memang awal naratif
scene berikutnya, batas boleh berada SEBELUM dialog baru tersebut.
Jika gambar sudah berubah tetapi dialog/aksi lama masih berlanjut, batas boleh berada SETELAH
pergantian gambar.

Jangan mencoba memilih frame encoding/keyframe. MiniCut akan mengunci keputusan Anda ke frame
PTS nyata setelah Anda memberikan ZONA BATAS NARATIF.

Semua offset di JSON adalah MILIDETIK relatif terhadap pusat kandidat terpilih:
0 = tepat di pusat kandidat; negatif = sebelum; positif = sesudah.
Cuplikan tiap kandidat mencakup kira-kira ±{clip_radius_ms} ms dari pusat.

Kandidat lokal:
{chr(10).join(rows)}

Kembalikan HANYA JSON valid:
{{
  "selected_candidate_index": 1,
  "boundary_start_offset_ms": -800,
  "boundary_end_offset_ms": 500,
  "preferred_offset_ms": -120,
  "new_content_starts_offset_ms": 200,
  "cut_intent": "before_new_dialogue",
  "confidence": 0.0,
  "dialogue_safe": true,
  "action_safe": true,
  "j_or_l_cut": "j_cut|l_cut|none|unclear",
  "needs_review": false,
  "reason": "alasan singkat dalam Bahasa Indonesia"
}}

boundary_start/end = zona sempit yang secara naratif aman untuk cut.
preferred_offset_ms = posisi ideal di dalam zona tersebut.
new_content_starts_offset_ms = jika dapat dikenali, saat isi/dialog scene baru mulai; jika tidak
jelas gunakan null.
Jika tidak yakin, needs_review=true dan confidence rendah.
""".strip()
