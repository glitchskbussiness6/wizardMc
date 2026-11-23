import argparse
import json
import socket
import struct
import sys
import time
import cv2
import numpy as np

LOCAL_PORT_DEFAULT = 7070
WIN = "Remote Host Stream"
PAD_PX = 0
PAD_COLOR = (0, 0, 0)
GRIP_SIZE = 24
KEEP_ASPECT = True
MIN_W, MIN_H = 240, 160

IS_MACOS = sys.platform == "darwin"
running_flag = False


def send(sock, peer, obj):
    if peer is None:
        return
    try:
        sock.sendto(json.dumps(obj).encode("utf-8"), peer)
    except Exception:
        pass


def mouse_callback(event, x, y, flags, param):
    st, sock, peer_ref = param
    cw, ch = st["cw"], st["ch"]

    if event == cv2.EVENT_LBUTTONDOWN:
        if x >= cw - GRIP_SIZE and y >= ch - GRIP_SIZE:
            st["drag"] = {"sx": x, "sy": y, "sw": cw, "sh": ch}
            return

    if event == cv2.EVENT_MOUSEMOVE and st["drag"] and (flags & cv2.EVENT_FLAG_LBUTTON):
        d = st["drag"]
        dx, dy = x - d["sx"], y - d["sy"]
        if KEEP_ASPECT and st["aspect"]:
            a = st["aspect"]
            nw = d["sw"] + (dx + dy / a) / 2.0
            nh = nw * a
        else:
            nw, nh = d["sw"] + dx, d["sh"] + dy
        st["target"] = (max(MIN_W, int(nw)), max(MIN_H, int(nh)))
        return

    if event == cv2.EVENT_LBUTTONUP:
        st["drag"] = None
        return

    if st["drag"]:
        return

    if event not in (cv2.EVENT_LBUTTONDOWN, cv2.EVENT_RBUTTONDOWN):
        return
    ox, oy, dw, dh = st["ox"], st["oy"], st["dw"], st["dh"]
    if dw <= 0 or dh <= 0:
        return
    rx, ry = x - ox, y - oy
    if rx < 0 or ry < 0 or rx > dw or ry > dh:
        return
    norm_x = max(0.0, min(1.0, rx / float(dw)))
    norm_y = max(0.0, min(1.0, ry / float(dh)))
    button = 1 if event == cv2.EVENT_LBUTTONDOWN else 2
    send(sock, peer_ref[0], {"type": "mouse_click", "x": norm_x, "y": norm_y, "button": button})


def render(img, cw, ch, st):
    canvas = np.empty((ch, cw, 3), dtype=np.uint8)
    canvas[:] = PAD_COLOR

    if img is not None:
        h, w = img.shape[:2]
        st["aspect"] = h / float(w)
        aw, ah = max(1, cw - 2 * PAD_PX), max(1, ch - 2 * PAD_PX)
        scale = min(aw / float(w), ah / float(h))
        dw, dh = max(1, int(w * scale)), max(1, int(h * scale))
        interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
        small = cv2.resize(img, (dw, dh), interpolation=interp)
        ox = PAD_PX + (aw - dw) // 2
        oy = PAD_PX + (ah - dh) // 2
        canvas[oy : oy + dh, ox : ox + dw] = small
        st.update(ox=ox, oy=oy, dw=dw, dh=dh)
    else:
        cv2.putText(
            canvas,
            "Waiting for host...",
            (PAD_PX + 10, ch // 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.9,
            (255, 255, 255),
            2,
        )
        st.update(ox=0, oy=0, dw=0, dh=0)

    g = min(GRIP_SIZE, cw, ch)
    x0, y0 = cw - g, ch - g
    roi = canvas[y0:ch, x0:cw]
    roi[:] = (roi * 0.4).astype(np.uint8)
    for i in (g // 4, g // 2, 3 * g // 4):
        cv2.line(canvas, (cw - 3 - i, ch - 3), (cw - 3, ch - 3 - i), (255, 255, 255), 2)

    st["cw"], st["ch"] = cw, ch
    return canvas


def window_size(st):
    try:
        _, _, w, h = cv2.getWindowImageRect(WIN)
        if w > 50 and h > 50:
            return int(w), int(h)
    except Exception:
        pass
    return st["cw"], st["ch"]


def handle_packet(pkt, frames, last_shown_ref, last_complete_ref, last_img_ref):
    if len(pkt) < 8:
        return False
    fid, idx, total = struct.unpack("!IHH", pkt[:8])
    if total == 0 or idx >= total:
        return False

    if fid < last_shown_ref[0] and time.time() - last_complete_ref[0] > 2.0:
        last_shown_ref[0] = -1
        frames.clear()

    if fid <= last_shown_ref[0]:
        return False

    parts = frames.setdefault(fid, {})
    parts[idx] = pkt[8:]

    if len(parts) == total:
        data = b"".join(parts[i] for i in range(total))
        img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        for k in [k for k in frames if k <= fid]:
            del frames[k]
        if img is not None:
            last_img_ref[0] = img
            last_shown_ref[0] = fid
            last_complete_ref[0] = time.time()
            return True
    return False


def stop_viewer():
    global running_flag
    running_flag = False


def create_udp_socket(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    except OSError:
        pass
    if sys.platform == "win32":
        try:
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        except Exception:
            pass
    sock.bind(("0.0.0.0", port))
    sock.setblocking(False)
    return sock


def run_viewer(port=LOCAL_PORT_DEFAULT):
    global running_flag
    running_flag = True

    st = {
        "cw": 1280,
        "ch": 720,
        "ox": 0,
        "oy": 0,
        "dw": 1,
        "dh": 1,
        "drag": None,
        "target": None,
        "aspect": None,
    }
    frames = {}
    last_shown_ref = [-1]
    last_ping = 0.0
    last_complete_ref = [time.time()]
    last_img_ref = [None]
    peer_ref = [None]

    sock = create_udp_socket(port)

    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WIN, 1040 + 2 * PAD_PX, 620 + 2 * PAD_PX)
    cv2.setMouseCallback(WIN, mouse_callback, param=(st, sock, peer_ref))
    cv2.imshow(WIN, render(None, st["cw"], st["ch"], st))
    cv2.waitKey(30)

    announced = False

    try:
        while running_flag:
            now = time.time()
            if now - last_ping > 0.3:
                send(sock, peer_ref[0], {"type": "ping"})
                last_ping = now

            new_frame = False
            for _ in range(4000):
                try:
                    pkt, addr = sock.recvfrom(65535)
                    peer_ref[0] = addr
                    if not announced:
                        announced = True
                except (BlockingIOError, InterruptedError):
                    break
                except ConnectionResetError:
                    continue
                except Exception:
                    break
                try:
                    if handle_packet(pkt, frames, last_shown_ref, last_complete_ref, last_img_ref):
                        new_frame = True
                except Exception:
                    pass

            if len(frames) > 15:
                max_fid = max(frames.keys())
                frames = {k: v for k, v in frames.items() if max_fid - k < 3}

            if st["target"]:
                tw, th = st["target"]
                st["target"] = None
                try:
                    cv2.resizeWindow(WIN, tw, th)
                except Exception:
                    pass

            cw, ch = window_size(st)
            if new_frame or (cw, ch) != (st["cw"], st["ch"]):
                cv2.imshow(WIN, render(last_img_ref[0], cw, ch, st))

            key = cv2.waitKey(5) & 0xFF
            if key == 27:
                break
            elif key in (8, 127):
                send(sock, peer_ref[0], {"type": "key_press", "key": "Backspace"})
            elif key in (13, 10):
                send(sock, peer_ref[0], {"type": "key_press", "key": "Enter"})
            elif key == 9:
                send(sock, peer_ref[0], {"type": "key_press", "key": "Tab"})
            elif 32 <= key <= 126:
                send(sock, peer_ref[0], {"type": "key_text", "text": chr(key)})
    finally:
        cv2.destroyAllWindows()
        try:
            sock.close()
        except Exception:
            pass
        running_flag = False


def parse_args():
    parser = argparse.ArgumentParser(description="Remote Host Stream UDP Viewer (macOS)")
    parser.add_argument("local_port", nargs="?", type=int, default=LOCAL_PORT_DEFAULT)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_viewer(args.local_port)