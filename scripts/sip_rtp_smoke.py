"""Local end-to-end SIP/RTP smoke test for the optional ChatGPT voice gateway.

Uses only the standard library plus the gateway's PCMU/RTP helpers. Pass a
speech WAV with --wav for a meaningful spoken turn; without one it sends a
440 Hz transport tone to verify packet flow and codec handling.
"""

from __future__ import annotations

import argparse
import math
import re
import socket
import struct
import sys
import time
import uuid
import wave

from chatgpt_api.providers.chatgpt.sip_gateway import decode_pcmu, encode_pcmu, make_rtp, parse_rtp


def _response(data: bytes) -> tuple[int, dict[str, str], bytes]:
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise ValueError("respuesta SIP inválida")
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return int(parts[1]), headers, body


def _wav_pcmu(path: str | None, tone_seconds: int) -> list[bytes]:
    if path:
        with wave.open(path, "rb") as source:
            channels, rate, width = source.getnchannels(), source.getframerate(), source.getsampwidth()
            if source.getcomptype() != "NONE" or width not in (1, 2, 3, 4):
                raise ValueError("el WAV debe ser PCM sin compresión (8/16/24/32 bits)")
            raw = source.readframes(source.getnframes())
        stride = channels * width
        samples = []
        for offset in range(0, len(raw) - stride + 1, stride):
            total = 0
            for channel in range(channels):
                chunk = raw[offset + channel * width : offset + (channel + 1) * width]
                if width == 1:
                    value = (chunk[0] - 128) << 8
                else:
                    value = int.from_bytes(chunk, "little", signed=True) >> (8 * (width - 2))
                total += value
            samples.append(max(-32768, min(32767, total // channels)))
        mono_8k = [samples[min(int(index * rate / 8000), len(samples) - 1)] for index in range(int(len(samples) * 8000 / rate))] if samples else []
        pcm = b"".join(struct.pack("<h", sample) for sample in mono_8k)
        # Cap test input to 30 seconds so a malformed or huge WAV cannot
        # monopolize the local gateway.
        pcm = pcm[: 30 * 8000 * 2]
    else:
        pcm_bytes = bytearray()
        for sample_index in range(tone_seconds * 8000):
            sample = int(6500 * math.sin(2 * math.pi * 440 * sample_index / 8000))
            pcm_bytes.extend(struct.pack("<h", sample))
        pcm = bytes(pcm_bytes)
    return [encode_pcmu(pcm[index : index + 320].ljust(320, b"\0")) for index in range(0, len(pcm), 320)]


def run(args: argparse.Namespace) -> int:
    call_id = f"{uuid.uuid4()}@127.0.0.1"
    branch = f"z9hG4bK{uuid.uuid4().hex}"
    from_header = f"<sip:smoke@127.0.0.1>;tag={uuid.uuid4().hex[:12]}"
    target = f"sip:voice@{args.sip_host}:{args.sip_port}"
    local_ip = "127.0.0.1"
    sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sip.bind((local_ip, args.client_sip_port))
    rtp.bind((local_ip, args.client_rtp_port))
    sip.settimeout(args.timeout)
    rtp.settimeout(0.15)
    cseq = "1 INVITE"
    sdp = (f"v=0\r\no=smoke 1 1 IN IP4 {local_ip}\r\ns=SIP RTP smoke\r\n"
           f"c=IN IP4 {local_ip}\r\nt=0 0\r\nm=audio {args.client_rtp_port} RTP/AVP 0\r\n"
           "a=rtpmap:0 PCMU/8000\r\na=ptime:20\r\na=sendrecv\r\n")
    invite = (f"INVITE {target} SIP/2.0\r\nVia: SIP/2.0/UDP {local_ip}:{args.client_sip_port};branch={branch}\r\n"
              f"Max-Forwards: 1\r\nFrom: {from_header}\r\nTo: <{target}>\r\nCall-ID: {call_id}\r\n"
              f"CSeq: {cseq}\r\nContact: <sip:smoke@{local_ip}:{args.client_sip_port}>\r\n"
              f"Content-Type: application/sdp\r\nContent-Length: {len(sdp)}\r\n\r\n{sdp}").encode("ascii")
    gateway = (args.sip_host, args.sip_port)
    try:
        sip.sendto(invite, gateway)
        final = None
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            packet, _ = sip.recvfrom(65535)
            status, headers, body = _response(packet)
            if status >= 200:
                final = (status, headers, body)
                break
        if not final or final[0] != 200:
            print(f"SIP INVITE: {final[0] if final else 'sin respuesta'}")
            return 1
        status, headers, body = final
        peer_match = re.search(rb"(?m)^c=IN IP4 ([^\r\n]+)", body)
        port_match = re.search(rb"(?m)^m=audio (\d+) RTP/AVP", body)
        if not peer_match or not port_match:
            raise ValueError("la respuesta SIP 200 no incluye el destino RTP")
        rtp_peer = (peer_match.group(1).decode("ascii"), int(port_match.group(1)))
        to_header = headers.get("to", f"<{target}>")
        ack = (f"ACK {target} SIP/2.0\r\nVia: SIP/2.0/UDP {local_ip}:{args.client_sip_port};branch=z9hG4bK{uuid.uuid4().hex}\r\n"
               f"From: {from_header}\r\nTo: {to_header}\r\nCall-ID: {call_id}\r\nCSeq: 1 ACK\r\n"
               "Content-Length: 0\r\n\r\n").encode("ascii")
        sip.sendto(ack, gateway)

        packets = _wav_pcmu(args.wav, args.tone_seconds)
        sent = 0
        started = time.monotonic()
        for sequence, payload in enumerate(packets):
            rtp.sendto(make_rtp(sequence, sequence * 160, 0x534D4F4B, payload), rtp_peer)
            sent += 1
            target_time = started + (sequence + 1) * 0.02
            time.sleep(max(0, target_time - time.monotonic()))

        received = peak = 0
        end = time.monotonic() + args.listen_seconds
        while time.monotonic() < end:
            try:
                data, _ = rtp.recvfrom(4096)
            except TimeoutError:
                continue
            parsed = parse_rtp(data)
            if parsed:
                received += 1
                pcm = decode_pcmu(parsed[2])
                peak = max(peak, max((abs(value[0]) for value in struct.iter_unpack("<h", pcm)), default=0))

        bye = (f"BYE {target} SIP/2.0\r\nVia: SIP/2.0/UDP {local_ip}:{args.client_sip_port};branch=z9hG4bK{uuid.uuid4().hex}\r\n"
               f"From: {from_header}\r\nTo: {to_header}\r\nCall-ID: {call_id}\r\nCSeq: 2 BYE\r\n"
               "Content-Length: 0\r\n\r\n").encode("ascii")
        sip.sendto(bye, gateway)
        bye_status = None
        try:
            while True:
                reply, _ = sip.recvfrom(65535)
                bye_status, _, _ = _response(reply)
                if bye_status >= 200:
                    break
        except TimeoutError:
            pass

        print(f"SIP INVITE: {status} OK")
        print(f"RTP PCMU enviados: {sent} paquetes; recibidos: {received}; pico PCM: {peak}")
        print(f"SIP BYE: {bye_status if bye_status is not None else 'sin respuesta'}")
        if received == 0 or bye_status != 200:
            print("No se completó el transporte de retorno o el cierre SIP; revisa la cuenta y los registros del gateway.")
            return 1
        if peak == 0:
            print("Transporte SIP/RTP correcto; el tono no generó voz. Usa --wav con voz hablada para validar audio de respuesta.")
        return 0
    finally:
        sip.close()
        rtp.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Prueba local de señalización SIP y audio RTP/PCMU")
    parser.add_argument("--sip-host", default="127.0.0.1")
    parser.add_argument("--sip-port", type=int, default=5066)
    parser.add_argument("--client-sip-port", type=int, default=5068)
    parser.add_argument("--client-rtp-port", type=int, default=18008)
    parser.add_argument("--wav", help="WAV PCM hablado para probar el retorno de voz real")
    parser.add_argument("--tone-seconds", type=int, default=3)
    parser.add_argument("--listen-seconds", type=int, default=15)
    parser.add_argument("--timeout", type=int, default=100)
    args = parser.parse_args()
    try:
        raise SystemExit(run(args))
    except (OSError, ValueError, wave.Error) as exc:
        print(f"Prueba SIP/RTP fallida: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
