import os
import json
import time
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

AK = "HPUAPVGJ1KKYZLEY62YG"
SK = "2C8jS6k6gJgZL6sO5NsPP3Ue0BQqTJihVl7Z7B51"
PROJECT_ID = "019e2a94943f735c96a6e968914f7896"
INSTANCE_ID = "909e5ebd-3547-423f-bbfe-738f09345e24"
DEVICE_ID = "6a2930e2cbb0cf6bb9639250_esp32_001"
REGION = "cn-north-4"
SERVICE_ID = "cyt"

IAM_ENDPOINT = f"https://iam.{REGION}.myhuaweicloud.com"
IOTDA_ENDPOINT = "https://d63c160ec2.st1.iotda-app.cn-north-4.myhuaweicloud.com"

_token_cache = {"token": None, "expires_at": 0}
_history = []
MAX_HISTORY = 200

home_mode = 'home'
last_motion_state = 0
last_temp_alert_time = 0
last_motion_alert_time = 0
ALERT_COOLDOWN = 120


def send_wechat(title, content):
    url = f"https://bemfa.com/api/beinfo?uid=92bb961dba86472d9fcd1b16cd4b3871&title={title}&content={content}"
    try:
        requests.get(url, timeout=5)
        print(f"[微信推送] {title}: {content}")
    except Exception as e:
        print(f"[微信推送失败] {e}")


@app.route("/")
def index():
    return app.send_static_file("frontend.html")


def get_iam_token():
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 300:
        return _token_cache["token"]

    resp = requests.post(
        f"{IAM_ENDPOINT}/v3/auth/tokens",
        json={
            "auth": {
                "identity": {
                    "methods": ["hw_ak_sk"],
                    "hw_ak_sk": {
                        "access": {"key": AK},
                        "secret": {"key": SK}
                    }
                },
                "scope": {
                    "project": {"id": PROJECT_ID}
                }
            }
        },
        timeout=10,
    )

    if resp.status_code != 201:
        raise Exception(f"IAM Token 失败 [{resp.status_code}]: {resp.text}")

    token = resp.headers.get("X-Subject-Token")
    if not token:
        raise Exception("响应中无 X-Subject-Token")

    try:
        from datetime import datetime
        expires_ts = datetime.fromisoformat(
            resp.json()["token"]["expires_at"].replace("Z", "+00:00")
        ).timestamp()
    except Exception:
        expires_ts = now + 86400

    _token_cache["token"] = token
    _token_cache["expires_at"] = expires_ts
    return token


def iotda(method, path, body=None, params=None):
    headers = {
        "Content-Type": "application/json",
        "X-Auth-Token": get_iam_token(),
        "Instance-Id": INSTANCE_ID,
    }
    data = json.dumps(body, ensure_ascii=False).encode() if body else None
    return requests.request(
        method,
        f"{IOTDA_ENDPOINT}{path}",
        headers=headers,
        data=data,
        params=params,
        timeout=10
    )


def send_message(name, message_dict):
    path = f"/v5/iot/{PROJECT_ID}/devices/{DEVICE_ID}/messages"
    body = {
        "message_id": f"{name}-{int(time.time() * 1000)}",
        "name": name,
        "message": message_dict,
        "encoding": "none",
        "payload_format": "standard",
        "topic_full_name": f"$oc/devices/{DEVICE_ID}/sys/messages/down",
    }
    return iotda("POST", path, body)


@app.route("/light", methods=["POST", "OPTIONS"])
def light():
    if request.method == "OPTIONS":
        return _cors()
    status = int(request.get_json(force=True).get("LightStatus", 0))
    try:
        r = send_message("light-control", {"LightStatus": status})
        if r.status_code in (200, 201):
            return jsonify({"ok": True, "LightStatus": status})
        return jsonify({"ok": False, "status_code": r.status_code, "error": _safe_json(r)}), 502
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/command", methods=["POST", "OPTIONS"])
def command():
    global home_mode
    if request.method == "OPTIONS":
        return _cors()
    params = request.get_json(force=True)

    if "HomeMode" in params:
        home_mode = params["HomeMode"]
        print(f"[安防] 模式切换为: {'在家模式' if home_mode == 'home' else '外出模式'}")
        return jsonify({"ok": True, "home_mode": home_mode})

    try:
        r = send_message("general-command", params)
        if r.status_code in (200, 201):
            return jsonify({"ok": True, "params": params})
        return jsonify({"ok": False, "status_code": r.status_code, "error": _safe_json(r)}), 502
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/sensor/latest", methods=["GET"])
def sensor_latest():
    global home_mode, last_motion_state, last_temp_alert_time, last_motion_alert_time
    try:
        r = iotda("GET", f"/v5/iot/{PROJECT_ID}/devices/{DEVICE_ID}/shadow")
        if r.status_code != 200:
            return jsonify({"ok": False, "error": r.text}), 502

        shadow = r.json().get("shadow", [])
        result = {
            "temperature": None, "humidity": None,
            "soil": None, "motion": None,
            "LightStatus": None, "Alarm": None,
            "Relay": None, "ts": None,
            "home_mode": home_mode,
        }

        for svc in shadow:
            if svc.get("service_id") == SERVICE_ID:
                props = svc.get("reported", {}).get("properties", {})
                result["temperature"] = props.get("temperature")
                result["humidity"] = props.get("humidity")
                result["soil"] = props.get("soil")
                result["motion"] = props.get("motion")
                result["LightStatus"] = props.get("LightStatus")
                result["Alarm"] = props.get("Alarm")
                result["Relay"] = props.get("Relay")
                result["ts"] = svc.get("reported", {}).get("event_time")
                break

        # ========== 联动逻辑 ==========
        current_motion = result.get("motion", 0)
        current_temp = result.get("temperature")
        now = time.time()

        if current_motion == 1 and last_motion_state == 0:
            print(f"[联动] 检测到人！当前模式: {home_mode}")
            if home_mode == 'home':
                print("[联动] 在家模式：自动开灯")
                send_message("auto-light", {"LightStatus": 1})
            else:
                print("[联动] 外出模式：触发报警")
                send_message("auto-alarm", {"Alarm": True})
                if now - last_motion_alert_time > ALERT_COOLDOWN:
                    send_wechat("安防警报", "检测到有人闯入，请注意！")
                    last_motion_alert_time = now

        if current_temp is not None and current_temp > 30:
            if now - last_temp_alert_time > ALERT_COOLDOWN:
                send_wechat("温度警告", f"当前温度{current_temp}°C，已超过30°C！")
                last_temp_alert_time = now

        last_motion_state = current_motion
        # ===================================

        if result["temperature"] is not None and result["ts"] is not None:
            if not _history or _history[-1]["ts"] != result["ts"]:
                _history.append({
                    "ts": result["ts"],
                    "temperature": result["temperature"],
                    "humidity": result["humidity"],
                    "soil": result["soil"],
                })
                if len(_history) > MAX_HISTORY:
                    _history.pop(0)

        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/sensor/history", methods=["GET"])
def sensor_history():
    limit = request.args.get("limit", 50, type=int)
    data = _history[-limit:] if len(_history) > limit else _history[:]
    return jsonify({"ok": True, "data": data, "total": len(_history)})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "running",
        "token_cached": bool(_token_cache["token"]),
        "ak": AK[:6] + "***"
    })


def _safe_json(resp):
    try:
        return resp.json()
    except:
        return resp.text


def _cors():
    r = app.make_response("")
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return r


if __name__ == "__main__":
    print("[系统] 后端启动中...")
    print("[系统] 安防联动+微信推送已启用")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)