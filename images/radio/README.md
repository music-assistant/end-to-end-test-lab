# Mock radio

No image of its own: runs without a music provider add a `radio` service on the players image (`lab/players:latest`) that runs `python3 -m labctl.radio` from the run's `/src` copy, so it needs no rebuild. Scenarios reach it at `LAB_RADIO_URL` (`http://radio:8000`). It streams manifest tone `a2t4` in a 25 s loop.

- `GET /icy`: endless 128 kb/s MP3 at real-time pace; with `Icy-MetaData: 1` a metadata block every 16000 bytes, title `Lab Radio <n>` changing every 10 s.
- `GET /hls/live.m3u8`, `GET /hls/<segment>.ts`: live AAC HLS, 4 s segments, sliding window of 6.
- `GET /flaky`: 403 on the first request after start or reset, then like `/icy`.
- `POST /control/drop` closes every `/icy` and `/flaky` stream; `POST /control/reset` re-arms `/flaky`; `GET /control/state` returns `title_index`, `connections`, `flaky_armed`; `GET /health` is 200 once the MP3 and three HLS segments exist.
