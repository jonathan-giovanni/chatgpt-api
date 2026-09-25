import asyncio

from chatgpt_api.providers.chatgpt.sip_gateway import (
    Call,
    SipGateway,
    decode_pcmu,
    encode_pcmu,
    make_rtp,
    parse_rtp,
    parse_sip,
    sip_audio_endpoint,
)


def _invite(ip="127.0.0.1", codec="0"):
    sdp = (
        f"v=0\r\no=- 0 0 IN IP4 {ip}\r\ns=test\r\nc=IN IP4 {ip}\r\nt=0 0\r\n"
        f"m=audio 18000 RTP/AVP {codec}\r\na=rtpmap:0 PCMU/8000\r\n"
    )
    return (
        "INVITE sip:voice@127.0.0.1 SIP/2.0\r\n"
        "Via: SIP/2.0/UDP 127.0.0.1:5062;branch=z9hG4bK123\r\n"
        "From: <sip:test@127.0.0.1>;tag=1\r\n"
        "To: <sip:voice@127.0.0.1>\r\n"
        "Call-ID: call-1\r\nCSeq: 1 INVITE\r\n"
        f"Content-Length: {len(sdp)}\r\n\r\n{sdp}"
    ).encode("ascii")


def test_sip_parser_accepts_pcmu_only_from_signalling_peer():
    request = parse_sip(_invite())
    assert request.method == "INVITE"
    assert sip_audio_endpoint(request, "127.0.0.1") == ("127.0.0.1", 18000)
    assert sip_audio_endpoint(request, "127.0.0.2") is None
    assert sip_audio_endpoint(parse_sip(_invite(codec="8")), "127.0.0.1") is None


def test_sip_parser_allows_private_sdp_ip_behind_docker_nat_only():
    request = parse_sip(_invite(ip="192.168.1.20"))
    assert sip_audio_endpoint(request, "172.20.0.1") == ("172.20.0.1", 18000)
    assert sip_audio_endpoint(parse_sip(_invite(ip="8.8.8.8")), "172.20.0.1") is None


def test_rtp_pcmu_round_trip_and_packet_validation():
    pcm = (b"\0\0" + (1000).to_bytes(2, "little", signed=True) +
           (-1000).to_bytes(2, "little", signed=True)) * 53 + b"\0\0"
    ulaw = encode_pcmu(pcm)
    assert len(ulaw) == len(pcm) // 2
    decoded = decode_pcmu(ulaw)
    assert len(decoded) == len(pcm)
    packet = make_rtp(65535, 1234, 5678, ulaw)
    assert parse_rtp(packet) == (65535, 1234, ulaw)
    assert parse_rtp(packet[:8]) is None
    assert parse_rtp(packet[:1] + b"\x08" + packet[2:]) is None


def test_sip_gateway_rejects_untrusted_peer_without_response():
    gateway = SipGateway(
        bridge_url="http://127.0.0.1:8000", api_key="test", listen_ip="127.0.0.1",
        advertise_ip="127.0.0.1", sip_port=5060, rtp_port=40000,
        allowed_ips=("127.0.0.1/32",), voice="cove", model="auto", project=None,
        conversation_id=None, text=None, files=[],
    )
    gateway._sip_packet(_invite(), ("198.51.100.2", 5060))
    assert gateway.call is None


def test_sip_gateway_accepts_local_microsip_registration():
    gateway = SipGateway(
        bridge_url="http://127.0.0.1:8000", api_key="test", listen_ip="127.0.0.1",
        advertise_ip="127.0.0.1", sip_port=5060, rtp_port=40000,
        allowed_ips=("127.0.0.1/32",), voice="cove", model="auto", project=None,
        conversation_id=None, text=None, files=[],
    )

    class Transport:
        packets = []

        def sendto(self, data, address):
            self.packets.append((data, address))

    gateway.sip_transport = Transport()
    request = (
        "REGISTER sip:127.0.0.1 SIP/2.0\r\n"
        "Via: SIP/2.0/UDP 127.0.0.1:5068;branch=z9hG4bKreg\r\n"
        "From: <sip:voice@127.0.0.1>;tag=1\r\n"
        "To: <sip:voice@127.0.0.1>\r\n"
        "Call-ID: register-1\r\nCSeq: 1 REGISTER\r\n"
        "Contact: <sip:voice@127.0.0.1:5068>\r\nExpires: 3600\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()
    gateway._sip_packet(request, ("127.0.0.1", 5068))
    response = gateway.sip_transport.packets[0][0]
    assert response.startswith(b"SIP/2.0 200 OK")
    assert response.count(b"Contact:") == 1
    assert b"Expires: 3600" in response


def test_sip_gateway_sends_bye_after_30_seconds_without_rtp():
    async def scenario():
        gateway = SipGateway(
            bridge_url="http://127.0.0.1:8000", api_key="test", listen_ip="127.0.0.1",
            advertise_ip="127.0.0.1", sip_port=5060, rtp_port=40000,
            allowed_ips=("127.0.0.1/32",), voice="cove", model="auto", project=None,
            conversation_id=None, text=None, files=[],
        )

        class Transport:
            packets = []

            def sendto(self, data, address):
                self.packets.append((data, address))

        gateway.sip_transport = Transport()
        request = parse_sip(_invite())
        call = Call(request, ("127.0.0.1", 5062), ("127.0.0.1", 18000))
        call.established = True
        call.last_rtp_at -= 31
        gateway.call = call
        await gateway._watch_rtp(call)
        assert gateway.call is None
        assert gateway.sip_transport.packets[0][0].startswith(b"BYE ")

    asyncio.run(scenario())


def test_sip_cancel_terminates_pending_invite():
    async def scenario():
        gateway = SipGateway(
            bridge_url="http://127.0.0.1:8000", api_key="test", listen_ip="127.0.0.1",
            advertise_ip="127.0.0.1", sip_port=5060, rtp_port=40000,
            allowed_ips=("127.0.0.1/32",), voice="cove", model="auto", project=None,
            conversation_id=None, text=None, files=[],
        )

        class Transport:
            packets = []

            def sendto(self, data, address):
                self.packets.append((data, address))

        gateway.sip_transport = Transport()

        async def pending(_call):
            await asyncio.Event().wait()

        gateway._start_call = pending
        gateway._sip_packet(_invite(), ("127.0.0.1", 5068))
        assert gateway.call is not None
        assert gateway.sip_transport.packets[0][0].startswith(b"SIP/2.0 100")
        cancel = _invite().replace(b"INVITE sip:", b"CANCEL sip:").replace(b"CSeq: 1 INVITE", b"CSeq: 1 CANCEL")
        gateway._sip_packet(cancel, ("127.0.0.1", 5068))
        await asyncio.sleep(0)
        assert gateway.call is None
        statuses = [packet.split(b" ", 2)[1] for packet, _ in gateway.sip_transport.packets]
        assert statuses == [b"100", b"200", b"487"]

    asyncio.run(scenario())
