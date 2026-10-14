import asyncio
import time
from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod
from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD

async def main():
    print("1. Conectando ao Go2 via Wi-Fi AP...")
    conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalAP)
    await conn.connect()
    print("   -> Conectado!")

    await asyncio.sleep(2.0)

    # 1. Garante que o robô está em pé e equilibrado
    print("2. Ativando modo de equilíbrio (StandUp)...")
    await conn.datachannel.pub_sub.publish_request_new(
        RTC_TOPIC["SPORT_MOD"],
        {"api_id": SPORT_CMD["StandUp"]}
    )
    await asyncio.sleep(3.0)

    # 2. Teste de Ação Visível (Cumprimento / Tchau)
    print("3. Executando gesto de Cumprimento (Hello)...")
    await conn.datachannel.pub_sub.publish_request_new(
        RTC_TOPIC["SPORT_MOD"],
        {"api_id": SPORT_CMD["Hello"]}
    )
    await asyncio.sleep(4.0)

    # 3. Teste de Movimento com velocidade um pouco maior (0.3 m/s)
    print("4. Testando caminhada para FRENTE (0.3 m/s)...")
    inicio = time.time()
    while time.time() - inicio < 3.0:
        await conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["SPORT_MOD"],
            {"api_id": SPORT_CMD["Move"], "parameter": {"x": 0.3, "y": 0.0, "z": 0.0}}
        )
        await asyncio.sleep(0.05) # Reenvio contínuo a 20Hz (a cada 50ms)

    # 4. Parada
    print("5. Parando o robô...")
    await conn.datachannel.pub_sub.publish_request_new(
        RTC_TOPIC["SPORT_MOD"],
        {"api_id": SPORT_CMD["Move"], "parameter": {"x": 0.0, "y": 0.0, "z": 0.0}}
    )
    await asyncio.sleep(1.0)

    print("6. Desconectando...")
    await conn.disconnect()
    print("   -> Teste finalizado!")

if __name__ == "__main__":
    asyncio.run(main())