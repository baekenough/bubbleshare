# 로컬 Langfuse 관측

에이전트의 모델 호출별 토큰·지연·실제 공급자 비용을 Langfuse에서 확인하기 위한 별도 Docker 스택이다. 앱과 분리된 `bubbleshare-langfuse` Compose 프로젝트를 사용한다.

Langfuse web/worker는 **3.225.11의 이미지 digest**로 고정했다. 이는 2026-10-15 Docker Hub에서 양쪽 이미지의 제공을 확인한 v3 버전이며, 전체 최신 메이저인 v4를 뜻하지 않는다. GitHub의 v3.225.12 릴리스는 확인했지만 동일 버전 worker 이미지 pull은 `not found`여서 사용하지 않았다. [공식 v3 Compose](https://github.com/langfuse/langfuse/blob/v3.225.11/docker-compose.yml)를 기준으로 PostgreSQL 17, ClickHouse 25.12, Redis 7, MinIO를 구성했다. 공식 구성에서 공개 포트와 시크릿 관리, health check를 이 로컬 환경에 맞췄다.

## 시작

```bash
python3 scripts/setup_langfuse.py
docker compose -f compose.langfuse.yaml config --quiet
docker compose -f compose.langfuse.yaml up -d --wait --wait-timeout 300
```

setup 스크립트는 `.env`의 기존 값을 보존하고 누락되거나 비어 있는 Langfuse 설정만 추가한다. 비밀번호·암호화 키·API 키는 무작위로 생성하며 내용을 출력하지 않는다. `.env` 권한은 `0600`으로 설정한다. 반복 실행해도 이미 설정한 키를 교체하지 않는다.

- UI: `http://localhost:3000`
- 로그인 이메일: `.env`의 `LANGFUSE_INIT_USER_EMAIL`
- 로그인 비밀번호: `.env`의 `LANGFUSE_INIT_USER_PASSWORD`
- 프로젝트 API 키: `.env`의 `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`

[공식 headless initialization](https://langfuse.com/self-hosting/administration/headless-initialization)에 따라 시작할 때 조직 `vivi-local`, 프로젝트 `vivi-agent`, 사용자와 프로젝트 API 키를 만든다. 초기화는 기존 리소스를 덮어쓰는 비밀번호 재설정 기능이 아니다. 자격정보를 변경하려면 이미 생성된 사용자·프로젝트의 설정도 함께 관리해야 한다.

앱 화면과 같은 머신에서 브라우저를 열어야 한다. web의 3000번과 MinIO의 9090번만 `127.0.0.1`에 게시하며, PostgreSQL·ClickHouse·Redis·worker·MinIO 관리 콘솔은 호스트 포트를 게시하지 않는다. 모든 데이터는 이 Compose 프로젝트의 named volume에 보존된다. 서버 텔레메트리와 외부 유료 평가 기능은 활성화하지 않았다.

## 앱에서 연결

호스트에서 실행하는 CLI/API의 설정은 다음 세 변수다. 값은 `.env`에 두며 키를 코드에 넣지 않는다.

```dotenv
LANGFUSE_BASE_URL=http://localhost:3000
LANGFUSE_PUBLIC_KEY=<generated in .env>
LANGFUSE_SECRET_KEY=<generated in .env>
```

컨테이너 안의 `localhost`는 해당 앱 컨테이너다. 앱을 이 스택의 `bubbleshare-langfuse` 네트워크에 연결하고 앱 컨테이너의 `LANGFUSE_BASE_URL`만 `http://langfuse-web:3000`으로 덮어쓴다. 브라우저 로그인에 쓰는 Langfuse web의 `NEXTAUTH_URL`은 호스트 주소인 `http://localhost:3000`을 유지한다. 기존 `compose.yaml`과 이 관측 스택은 별도 파일이며, 이 파일로 앱 이미지를 빌드하지 않는다.

앱용 `compose.observability.yaml`이 이 네트워크 연결과 주소 덮어쓰기를 제공한다. 관측 스택을 먼저 시작한 뒤 실행한다.

```bash
docker compose -f compose.yaml -f compose.observability.yaml up --build -d web
```

호스트에서 이미 8000번 API를 실행 중이면 해당 호스트 프로세스를 종료한 뒤 Docker web을 시작한다. CLI 컨테이너에도 동일한 overlay를 함께 전달한다.

수집 endpoint는 `POST /api/public/otel/v1/traces`이고 프로젝트 public/secret key로 인증한다. 비용은 공급자 응답에서 반환한 실제 USD를 기록해야 한다. 실제 비용이 없는 호출은 알 수 없음으로 남기며 토큰 수만으로 실측값을 만들지 않는다. 과제의 모델 티어 환산 비용 `$0.018`과 실제 공급자 비용은 별개다. Langfuse Docker의 CPU·메모리·저장 사용 또한 무료 원가로 가정해서는 안 되며, 과제 원가 모델에는 별도의 인프라 가정이 필요하다.

## 상태와 보존

```bash
docker compose -f compose.langfuse.yaml ps
curl --fail 'http://localhost:3000/api/public/health?failIfDatabaseUnavailable=true'
curl --fail http://localhost:3000/api/public/ready

# 일시 중지 / 다시 시작: 데이터 유지
docker compose -f compose.langfuse.yaml stop
docker compose -f compose.langfuse.yaml up -d --wait --wait-timeout 300

# 컨테이너 제거: named volume 데이터는 유지
docker compose -f compose.langfuse.yaml down
```

`down -v`는 관측 데이터를 삭제하므로 일반 종료에 사용하지 않는다. 다른 프로젝트의 컨테이너·볼륨은 관리하지 않는다. Compose의 일반 `config` 출력과 `docker inspect` 전체 출력은 환경변수의 비밀값을 포함할 수 있으므로 검증에는 `config --quiet` 또는 필요한 상태 필드만 사용한다.

포트를 변경하면 `.env`의 `LANGFUSE_WEB_PORT`와 `LANGFUSE_BASE_URL`을 함께 맞춘다. MinIO는 `LANGFUSE_MINIO_PORT`를 사용한다. 설정 변경 후 `up -d`로 컨테이너를 재생성한다.

## 기동 검증

2026-10-15 로컬에서 여섯 서비스 모두 `healthy`, `up --wait` 종료 코드 0을 확인했다. web의 DB 포함 health와 readiness가 HTTP 200을 반환했고, 생성한 프로젝트 API 키로 `GET /api/public/projects`에서 `vivi-agent`를 조회했다. web 컨테이너 재생성 뒤에도 같은 키와 프로젝트로 조회됐다. setup 재실행은 `.env` 내용을 변경하지 않았으며 권한 `0600`을 확인했다.

이미지의 기본 `HOSTNAME`이 컨테이너 이름으로 설정되어 loopback health가 실패하는 경우를 확인했으므로 web/worker에 `HOSTNAME=0.0.0.0`을 명시했다. 이 검증은 관측 인프라·API 인증에 대한 결과이며, 모델 호출별 실제 비용의 수집·대사는 별도 실제 호출 검증으로 확인한다.

## 실제 호출 영수증 대사

```bash
python3 scripts/verify_langfuse.py \
  output/product-v2-run1/lead.json \
  output/product-v2-run1/unsupported.json \
  output/product-v2-run1/security.json \
  output/product-v2-run2/lead.json \
  output/product-v2-run2/unsupported.json \
  output/product-v2-run2/security.json \
  output/product-v2-control/confirmed.json \
  output/product-v2-run3/lead.json \
  output/product-v2-run3/security.json \
  output/product-v2-run4/security.json
```

스크립트는 전달된 JSON의 `validation_receipt.observability`에 있는 trace/observation ID만 공식 v3 Public API로 조회한다. 각 호출에 generation이 정확히 한 개 있는지, inclusive 입력/출력 토큰과 분리된 cached/reasoning 토큰, 실제 비용, 모델·문제 범위·상태·지연이 영수증과 일치하는지 확인한다. 다른 테스트 trace와 프로젝트 전체 비용 합계를 섞지 않는다.

결과는 기본적으로 `output/product-v2-langfuse.json`에 저장한다. 추가 실험을 포함하려면 기존 실패 실험의 파일을 유지한 채 새 파일도 인자로 나열한다. 제품 결과 `fallback`과 공급자 호출의 `failed/completed` 상태는 별도이며, 실패한 유료 호출의 비용도 합산한다. 비동기 수집을 기다려야 하면 `--wait-seconds 60`을 추가한다. 이 스크립트는 조회만 수행하며 모델 호출이나 trace 재전송을 하지 않는다.

v2 검증의 실제 모델 호출 10/10건에서 영수증과 Langfuse 비용 합계가 **$1.803157**로 일치했다. 실패와 fallback에 사용된 비용도 포함하며, 독립 테스트 trace는 합계에서 제외했다. 호출마다 generation 한 개, 토큰 수, 모델·주제·질문·정책 metadata, 상태·지연을 대조했다. [v2 대사 결과](../output/product-v2-langfuse.json).

이후 비용 절감 비교의 20회도 모두 대사를 통과했으며 합계는 **$1.14256725**다. 품질 검토에서 제외한 설정의 비용도 포함한다. [비용 절감 실험 대사](../output/cost-optimization-langfuse.json), [설정별 비용·품질 결과](cost-optimization-results.md).

프롬프트와 생성 응답 본문은 Langfuse로 전송하지 않는다. 관측에는 모델·허용된 범위 식별자·해시·토큰·비용·상태·지연만 담으며 `content_captured=false`로 표시한다. 10건의 실제 조회 결과에서도 observation `input`과 `output`이 비어 있음을 확인했다. 원본 생성물과 검증 영수증은 로컬 실행 결과에서 별도로 확인한다.

여섯 Langfuse 컨테이너의 메모리 사용량 합계는 한 시점에 약 **3.03 GiB**였다([리소스 스냅샷](../output/langfuse-resource-snapshot.json)). 이는 로컬 순간 관측이며 월 클라우드 비용이나 평균·최대 사용량의 측정값이 아니다. 보존 기간·수집량·배포 환경을 정한 뒤 인프라 비용을 별도로 산정해야 한다.

참고: [공식 Docker 배포](https://langfuse.com/self-hosting/deployment/docker-compose), [health/readiness endpoint](https://langfuse.com/self-hosting/configuration/health-readiness-endpoints), [OpenTelemetry 수집](https://langfuse.com/integrations/native/opentelemetry).
