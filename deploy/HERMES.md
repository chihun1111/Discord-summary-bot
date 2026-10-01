# Hermes 관리 웹 배포

- 서버: `hermes@100.92.145.54` (Proxmox VM 102)
- 프로젝트: `/home/hermes/discord-chat-index-bot`
- Tailscale 내부 전용 주소: https://hermes.tail85b0de.ts.net
- 서비스: `discord-chat-admin.service`
- 내부 주소: `http://127.0.0.1:8765`
- 인증 해시 파일: `/home/hermes/.config/discord-chat-index-bot/admin-auth.json` (0600)

Tailscale에 연결한 기기에서 접속 시 브라우저의 HTTP Basic 로그인 창에서 관리자 계정을 입력합니다. 최초 생성한 계정 정보는 배포를 수행한 로컬 프로젝트의 `.deployment/hermes-access.txt`에만 보관합니다. 이 디렉터리는 Git 및 서버 소스 전송 대상에서 제외합니다. 서버에는 고엔트로피 비밀번호의 SHA-256 해시만 저장합니다.

관리 웹은 systemd로 부팅 시 시작하고 비정상 종료 시 재시작합니다. Discord 봇은 관리 웹의 **봇 설정**에서 토큰, 서버 ID, 채널 ID를 저장한 뒤 **봇 시작하기**로 시작합니다. 현재 봇 실행 여부는 메모리에만 유지되므로 관리 웹이나 서버를 재시작하면 봇을 다시 시작해야 합니다. Gemini 사용에는 별도 API 키와 AI 사용 설정이 필요합니다.

## 서버 운영

```bash
sudo systemctl status discord-chat-admin.service
sudo journalctl -u discord-chat-admin.service -n 50 --no-pager
sudo systemctl restart discord-chat-admin.service
tailscale serve status
```

`.env`와 `data/`는 서버에서 생성되는 운영 데이터입니다. 업데이트 시 덮어쓰거나 삭제하지 마세요. 업데이트에는 관리 웹 및 자식 봇의 재시작이 포함됩니다.

## 설치 구성

Python 3.12 가상환경에 `deploy/requirements-hermes.txt`의 검증 버전을 설치합니다. `deploy/discord-chat-admin.service`를 `/etc/systemd/system/`에 설치한 뒤 실행합니다.

```bash
sudo systemd-analyze verify /etc/systemd/system/discord-chat-admin.service
sudo systemctl daemon-reload
sudo systemctl enable --now discord-chat-admin.service
sudo tailscale serve --bg --yes --https=443 http://127.0.0.1:8765
```

Serve 설정은 재부팅 후 유지됩니다. 인터넷 공개 Funnel은 해제했습니다. Tailscale 네트워크에 접속하고 관리자 비밀번호로 인증해야 사용할 수 있습니다. Tailnet 전체 접근 정책은 변경하지 않았으며 관리 화면 자체에 별도 인증을 적용합니다. 앱은 지정된 HTTPS 주소의 Host/Origin을 검증합니다. [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve)

인증 파일 형식은 `{"username":"admin","password_sha256":"64자리 소문자 SHA-256 hex"}`입니다. 비밀번호 변경 시 암호학적으로 안전한 난수 비밀번호를 새로 생성하고 해시 파일을 교체한 뒤 서비스를 재시작합니다. 짧거나 재사용한 비밀번호의 해시를 사용하지 마세요. 인증 정보는 `.env`와 별도로 보관하며 봇 설정 API에서 조회하거나 변경할 수 없습니다.

변경 전 Funnel 설정은 서버의 `/home/hermes/.config/discord-chat-index-bot/funnel-before.json`에 보관했습니다. 이전 연결 대상은 `http://127.0.0.1:4173`이었고 해당 포트의 서비스는 실행되지 않았습니다.
