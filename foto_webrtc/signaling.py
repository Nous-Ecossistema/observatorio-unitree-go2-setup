"""Adapter HTTP para o handshake LocalAP do driver 2.2.0.

O SDK faz POSTs sem timeout. Reutilizamos seus helpers de autenticação,
mas controlamos a sessão HTTP e os timeouts, sem modificar globals do SDK.
Esta chamada síncrona é executada fora do event loop RTC por to_thread().
"""

import base64
import json


def exchange_local_sdp(ip, sdp, aes_128_key=None, timeout=5.0):
    import requests
    from unitree_webrtc_connect import unitree_auth as auth

    # IP local fixo: não usa proxies herdados de um ambiente corporativo.
    with requests.Session() as session:
        session.trust_env = False

        def post(port, path, body=None, headers=None):
            response = session.post(f"http://{ip}:{port}/{path}", data=body, headers=headers,
                                    timeout=(min(2.0, timeout), timeout))
            response.raise_for_status()
            if response.status_code != 200:
                raise ConnectionError(f"Handshake HTTP: código {response.status_code}")
            return response.text

        if auth._probe_tcp_port(ip, 9991, timeout=min(1.5, timeout)):
            notify = json.loads(base64.b64decode(post(9991, "con_notify")).decode("utf-8"))
            data = notify["data1"]
            if notify.get("data2") == 2:
                data = auth._decrypt_data1_legacy(data)
            elif notify.get("data2") == 3:
                data = auth._decrypt_data1_v3(data, aes_128_key)
            public_key = auth.rsa_load_public_key(data[10:-10])
            path = auth._calc_local_path_ending(data)
            session_key = auth.generate_aes_key()
            body = json.dumps({"data1": auth.aes_encrypt(sdp, session_key),
                               "data2": auth.rsa_encrypt(session_key, public_key)})
            answer = post(9991, f"con_ing_{path}", body,
                          {"Content-Type": "application/x-www-form-urlencoded"})
            return auth.aes_decrypt(answer, session_key)
        if auth._probe_tcp_port(ip, 8081, timeout=min(1.5, timeout)):
            return post(8081, "offer", sdp, {"Content-Type": "application/json"})
        raise auth.LocalSignalingPortError(ip)
