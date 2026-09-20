# MiniCut Studio + AI Agent

Integrasi agent langsung ke MiniCut Studio, dibuat modular agar mudah dibaca dan diperbaiki oleh manusia maupun AI.

## Arsitektur

- `minicut_agent/core.py` — proyek JSON, keyframe, pembagian part, FFmpeg.
- `minicut_agent/agent.py` — planner dan Tool Registry deterministik.
- `minicut_agent/bridge.py` — bridge lokal HTTP di 127.0.0.1:8765.
- `minicut_agent/workers.py` — pekerjaan background agar UI tidak macet.
- `minicut_agent/ui.py` — UI PySide6 dan penghubung seluruh modul.
- `mcp_server.py` — companion MCP stdio untuk agent eksternal.

## Kemampuan agent

Tool yang tersedia: get_state, seek, play, pause, add_cut, remove_cut, clear_cuts, divide_equal, divide_interval, save_project, export_all, undo.

Agent tidak boleh mengubah timeline secara bebas. Semua tindakan masuk melalui Tool Registry yang sama dengan bridge dan MCP.

## Mode

**Lokal · tanpa API** memahami perintah dasar seperti:
- bagi jadi 8 part
- tiap 10 menit
- tambah cut di 00:12:30 dan 00:25:00
- hapus semua cut

**OpenAI-compatible API** hanya mengirim instruksi pengguna dan state teks proyek untuk meminta rencana JSON. File video tidak dikirim oleh MiniCut.

## Build Windows

Workflow: `.github/workflows/minicut-agent-build.yml`.

Hasil build berupa folder **MiniCut Studio Agent** yang berisi aplikasi, companion MCP, dan FFmpeg/ffprobe bila instalasi FFmpeg pada runner berhasil.

## Prinsip keamanan

- Bridge hanya bind ke localhost 127.0.0.1.
- Agent membuat rencana lebih dulu lalu pengguna menekan Terapkan.
- Tool yang mengubah timeline mempunyai Undo.
- API key hanya berada pada memori UI dan tidak disimpan oleh aplikasi ini.
