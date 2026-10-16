"""Consulta somente leitura da API de modo do Go2 para diagnóstico."""

import asyncio
import json
import os
import sys

from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod
from unitree_webrtc_connect.constants import RTC_TOPIC


async def main():
    connection = UnitreeWebRTCConnection(
        WebRTCConnectionMethod.LocalAP,
        aes_128_key=os.environ.get("UNITREE_AES_128_KEY"),
    )
    try:
        await asyncio.wait_for(connection.connect(), timeout=35)
        response = await asyncio.wait_for(
            connection.datachannel.pub_sub.publish_request_new(
                RTC_TOPIC["MOTION_SWITCHER"], {"api_id": 1001}
            ),
            timeout=8,
        )
        print(json.dumps(response, ensure_ascii=False, indent=2, default=str))
    except TimeoutError as error:
        print(f"TIMEOUT: o Go2 não respondeu à consulta de modo: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    finally:
        await connection.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
