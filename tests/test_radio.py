"""Mock radio: ICY framing, title schedule, /flaky and the control endpoints."""

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from labctl import radio


def test_icy_metadata_block() -> None:
    """The block is the padded StreamTitle prefixed by its length in 16-byte units."""
    block = radio.icy_metadata_block("Lab Radio 1")
    text = b"StreamTitle='Lab Radio 1';"
    assert block[0] == 2
    assert block[1:] == text + b"\x00" * (32 - len(text))
    assert radio.icy_metadata_block("") == b"\x00"


def test_title_schedule() -> None:
    """The title changes every 10 s."""
    assert radio.title_at(0) == radio.title_at(9.9) == "Lab Radio 0"
    assert radio.title_at(10) == "Lab Radio 1"


async def test_flaky_then_icy() -> None:
    """/flaky refuses once, streams after that, and refuses again after a reset."""
    mp3 = bytes(radio.FRAME_BYTES * 100)
    async with TestClient(TestServer(radio.build_app(mp3, None))) as client:
        assert (await client.get("/flaky")).status == 403
        response = await client.get("/flaky", headers={"Icy-MetaData": "1"})
        assert response.status == 200
        assert response.headers["icy-metaint"] == "16000"
        audio = await response.content.readexactly(radio.ICY_METAINT)
        assert audio == bytes(radio.ICY_METAINT)
        assert (await response.content.readexactly(1))[0] > 0
        state = await (await client.get("/control/state")).json()
        assert state == {"title_index": 0, "connections": 1, "flaky_armed": False}
        assert (await (await client.post("/control/drop")).json()) == {"dropped": 1}
        await client.post("/control/reset")
        assert (await client.get("/flaky")).status == 403


async def test_icy_bursts_on_connect() -> None:
    """A new listener gets the burst at once instead of waiting for real-time pacing."""
    mp3 = bytes(radio.FRAME_BYTES * 100)
    async with TestClient(TestServer(radio.build_app(mp3, None))) as client:
        response = await client.get("/icy")
        burst = await asyncio.wait_for(response.content.readexactly(radio.BURST_BYTES), 1.0)
        assert len(burst) == radio.BURST_BYTES


async def test_tone_clip() -> None:
    """/tone/<id>.mp3 serves the encoded clip whole, unknown ids are not found."""
    clip = bytes(range(256)) * 4
    app = radio.build_app(bytes(radio.FRAME_BYTES), None, {"a4t1": clip})
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/tone/a4t1.mp3")
        assert response.status == 200
        assert response.headers["Content-Type"] == "audio/mpeg"
        assert response.headers["Content-Length"] == str(len(clip))
        assert await response.read() == clip
        assert (await client.get("/tone/a3t1.mp3")).status == 404
