#!/usr/bin/env python3
"""Zamanı gelmiş bildirimleri FCM'e gönderir.

GitHub Actions içinde beş dakikada bir çalışır. Panelden üretilen
`notifications/notifications.json` dosyasını okur, gönderim saati geçmiş ve
henüz gönderilmemiş kayıtları bulur, FCM HTTP v1 ile konuya yollar ve kaydı
`sentAt` ile işaretler.

TASARIM KARARLARI
-----------------
* **Saat UTC tutulur.** Panel kullanıcıya İstanbul saatini gösterir ama dosyaya
  UTC yazar. Yaz saati/saat dilimi karışıklığı böyle biter.
* **Aynı bildirim iki kez gitmez.** `sentAt` dolu olan atlanır; dosya her
  çalıştırmadan sonra depoya geri yazılır.
* **Geçmişte kalmış bildirim gönderilmez.** İş bir gün durursa dünkü bildirim
  bugün kullanıcıya düşmemeli; `maxDelayMinutes` sınırını aşan kayıt
  "kaçırıldı" diye işaretlenir.
* **Kısmi başarısızlık dosyayı bozmaz.** Bir kayıt hata alırsa diğerleri
  gönderilir, hatalı olan sonraki çalıştırmada yeniden denenir.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Dosya iki yerde durabiliyor: uygulama deposunda `notifications/` altında,
# yayın deposunda ise doğrudan `nerede-uygun/` içinde. Betik tek kopya olsun
# diye ikisini de tanır — yol uyuşmazlığı sessizce "gönderilecek bildirim yok"
# sonucunu verirdi ve fark edilmezdi.
_CANDIDATES = [
    ROOT / "notifications" / "notifications.json",
    ROOT / "notifications.json",
]
PATH = next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[0])

# Bu süreden daha eski bir bildirim artık gönderilmez.
MAX_DELAY = timedelta(minutes=90)

SCOPES = ["https://www.googleapis.com/auth/firebase.messaging"]


def fatal(msg: str) -> None:
    print(f"HATA: {msg}", file=sys.stderr)
    sys.exit(1)


def load_credentials():
    raw = os.environ.get("FCM_SERVICE_ACCOUNT", "").strip()
    if not raw:
        fatal("FCM_SERVICE_ACCOUNT gizli değişkeni tanımlı değil.")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        fatal(f"FCM_SERVICE_ACCOUNT geçerli JSON değil: {e}")
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=SCOPES
    )
    creds.refresh(Request())
    return creds, info["project_id"]


def send(project_id: str, token: str, topic: str, n: dict) -> tuple[bool, str]:
    """Tek bildirimi konuya gönderir. (başarı, açıklama) döner."""
    message = {
        "topic": topic,
        "notification": {"title": n["title"], "body": n["body"]},
        # Dokunulduğunda açılacak adres uygulamanın derin bağlantı yoluna girer.
        "data": {k: v for k, v in (("link", n.get("link") or ""),) if v},
        "apns": {
            "payload": {"aps": {"sound": "default", "badge": 1}},
            # Bildirim kullanıcıya görünecek; iOS'ta yüksek öncelik gerekir.
            "headers": {"apns-priority": "10"},
        },
        "android": {"priority": "high"},
    }
    res = requests.post(
        f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=UTF-8"},
        data=json.dumps({"message": message}, ensure_ascii=False).encode("utf-8"),
        timeout=30,
    )
    if res.status_code == 200:
        return True, res.json().get("name", "")
    return False, f"HTTP {res.status_code}: {res.text[:300]}"


def main() -> None:
    if not PATH.exists():
        print("Bildirim dosyası yok, yapacak iş yok.")
        return

    data = json.loads(PATH.read_text(encoding="utf-8"))
    items = data.get("notifications", [])
    topic = data.get("topic", "tum_kullanicilar")

    now = datetime.now(timezone.utc)
    due = []
    for n in items:
        if n.get("sentAt") or n.get("status") in ("sent", "missed", "cancelled"):
            continue
        try:
            at = datetime.fromisoformat(n["sendAt"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            n["status"] = "invalid"
            n["error"] = "sendAt okunamadı"
            continue
        if at <= now:
            due.append((n, at))

    if not due:
        print(f"Zamanı gelen bildirim yok. ({len(items)} kayıt, şu an {now:%H:%M} UTC)")
        PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        return

    creds, project_id = load_credentials()
    token = creds.token
    gonderilen = 0

    for n, at in due:
        gecikme = now - at
        if gecikme > MAX_DELAY:
            n["status"] = "missed"
            n["error"] = f"{int(gecikme.total_seconds() // 60)} dk geç kalındı"
            print(f"  ATLANDI (çok geç): {n['title']}")
            continue

        ok, detail = send(project_id, token, topic, n)
        if ok:
            n["status"] = "sent"
            n["sentAt"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            n.pop("error", None)
            gonderilen += 1
            print(f"  ✅ {n['title']}")
        else:
            # Kayıt işaretlenmez; sonraki çalıştırmada yeniden denenir.
            n["error"] = detail
            print(f"  ❌ {n['title']} -> {detail}")

    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    print(f"Gönderilen: {gonderilen}/{len(due)}")


if __name__ == "__main__":
    main()
