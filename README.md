# AILectureGen — 설치부터 실행까지

강의자료를 올리면 대본을 만들고, PPT와 강의 영상을 만드는 프로그램입니다.

이 문서는 **아나콘다를 처음 쓰는 사람**도 따라 할 수 있게 썼습니다. 한 단계씩 그대로 하면 됩니다.

회색 칸의 명령은 **지금 창이 어디를 가리키든** 그대로 복사해 붙여 넣으면 됩니다. 폴더를 찾아 들어갈 필요는 없습니다.

자세한 설계는 `MASTER_SPEC.md`를 보면 됩니다.

---

## 이 프로그램으로 할 수 있는 일

1. **강의자료로 만들기** — PDF, PPTX, DOCX, TXT, Markdown을 올리면 강의 구조와 대본을 만듭니다.
2. **만든 PPT로 대본·영상** — 이미 만든 PPTX를 그대로 쓰고, 대본과 강의 영상만 만듭니다.
3. 고급 옵션에서 **강의 영상 앞에 인트로**를 넣을 수 있습니다.

화면은 브라우저에서 열고, 실제 처리는 컴퓨터에서 돌아가는 두 프로그램이 담당합니다.

| 이름 | 하는 일 | 주소 |
|---|---|---|
| 백엔드 | 파일 분석, 대본, PPT, 영상 | http://127.0.0.1:8000 |
| 프론트엔드 | 우리가 보는 화면 | http://localhost:5173 |

둘 다 켜 두어야 화면이 동작합니다.

---

## 준비물

| 프로그램 | 왜 필요한가 | 필수 |
|---|---|---|
| [Anaconda](https://www.anaconda.com/download) | 파이썬을 설치하고 환경을 나눕니다 | 필수 |
| [Node.js LTS](https://nodejs.org/) | 화면(프론트엔드)을 설치·실행합니다 | 필수 |
| [Git](https://git-scm.com/download/win) | GitHub에서 프로젝트를 받아 옵니다 | 필수 |
| 이 프로젝트 폴더 | `git clone`으로 받는 `lecture` 폴더 | 필수 |
| OpenAI 등 API 키 | 대본을 더 자연스럽게 쓸 때 | 선택 |
| Microsoft PowerPoint | 강의 영상을 만들 때 슬라이드 화면을 더 잘 뽑습니다 | 선택 |

인터넷이 되는 Windows 컴퓨터를 기준으로 설명합니다.

---

## 1. 아나콘다 설치

1. 브라우저에서 [Anaconda 다운로드](https://www.anaconda.com/download)를 엽니다.
2. **Windows 64-Bit** 설치 파일을 받습니다.
3. 받은 파일을 실행합니다.
4. 설치 중 아래처럼 고르면 됩니다.
   - 사용권 동의: **I Agree**
   - 설치 대상: **Just Me**
   - 설치 위치: 기본값 그대로
   - **Add Anaconda to my PATH** 는 체크하지 않아도 됩니다. 대신 아래에서 **Anaconda Prompt**를 씁니다.
5. 설치가 끝나면 **마침**을 누릅니다.
6. 시작 메뉴에서 **Anaconda Prompt**를 찾아 엽니다.

프롬프트에 아래처럼 보이면 성공입니다.

```text
(base) C:\Users\이름>
```

`(base)`는 아나콘다의 기본 환경입니다.

---

## 2. Node.js 설치

화면을 돌리려면 Node.js가 필요합니다.

1. [https://nodejs.org/](https://nodejs.org/) 에서 **LTS** 버튼을 눌러 설치 파일을 받습니다.
2. 설치 파일을 실행하고, 안내는 기본값 그대로 **Next**를 눌러 설치합니다.
3. **Anaconda Prompt를 닫았다가 다시 엽니다.** (새로 설치한 명령을 인식하게 하려고)
4. 아래를 입력하고 Enter를 누릅니다.

```bat
node -v
npm -v
```

버전 숫자가 두 줄로 나오면 성공입니다. 예: `v22.11.0`, `10.9.0`

---

## 3. Git으로 프로젝트 받기

코드는 GitHub에 있습니다. [ji-nyu/lecture](https://github.com/ji-nyu/lecture) 저장소를 컴퓨터로 복사합니다.

### Git이 있는지 확인

Anaconda Prompt에 입력합니다.

```bat
git --version
```

`git version 2...`처럼 숫자가 나오면 됩니다.

없다고 나오면 둘 중 하나를 합니다.

1. [Git for Windows](https://git-scm.com/download/win)를 받아 설치합니다. 안내는 기본값 그대로 **Next**를 누르면 됩니다. 설치가 끝나면 Anaconda Prompt를 **닫았다가 다시** 엽니다.
2. 또는 Anaconda Prompt에서 `conda install git -y`를 실행합니다.

### 저장소 복사 (clone)

바탕화면에 받으려면 아래를 **한 줄씩** 입력합니다.

```bat
cd /d %USERPROFILE%\Desktop
git clone https://github.com/ji-nyu/lecture.git
```

`git clone`은 GitHub에 있는 파일을 내 컴퓨터로 내려받는 명령입니다. 끝나면 바탕화면에 `lecture` 폴더가 생깁니다. 그 폴더 안으로 들어가지 않아도 됩니다. 다음 단계 명령이 위치를 알아서 찾습니다.

이미 바탕화면에 `lecture` 폴더가 있다면 이 단계는 건너뛰면 됩니다.

제대로 받았는지 확인하려면:

```bat
dir %USERPROFILE%\Desktop\lecture
```

`backend`, `frontend`, `requirements.txt`, `README.md`가 보이면 맞습니다.

---

## 4. 전용 파이썬 환경 만들기

다른 작업과 섞이지 않도록, 이 프로그램만 쓰는 환경을 만듭니다. **한 번만** 하면 됩니다.

```bat
conda create -n lecture python=3.12 -y
```

끝나면 환경을 켭니다.

```bat
conda activate lecture
```

프롬프트 앞에 `(lecture)`가 보이면 성공입니다.

다음에 컴퓨터를 다시 켜면 `conda activate lecture`만 다시 하면 됩니다.

---

## 5. 파이썬 패키지 설치

`(lecture)`가 켜진 상태에서 아래를 그대로 붙여 넣습니다.

```bat
conda activate lecture
pip install -r %USERPROFILE%\Desktop\lecture\requirements.txt
```

몇 분이 걸릴 수 있습니다. 마지막에 에러 없이 끝나면 됩니다.

설치가 중간에 실패하면 같은 명령을 **한 번 더** 실행해 보세요.

---

## 6. 화면(프론트엔드) 패키지 설치

같은 Anaconda Prompt에서 아래를 그대로 붙여 넣습니다.

```bat
cd /d %USERPROFILE%\Desktop\lecture\frontend
npm install
```

---

## 7. 설정 파일 (처음에는 건너뛰어도 됩니다)

API 키 없이도 **기본 실행은 가능**합니다. 다만 대본 품질은 키가 있을 때 더 좋아집니다.

키를 쓰려면:

1. `backend` 폴더 안에 `.env.example`이 있습니다.
2. 이 파일을 복사해서 같은 폴더에 `.env`라는 이름으로 저장합니다.
3. 메모장으로 `backend\.env`를 엽니다.
4. `LLM_API_KEY=` 뒤에 본인 키를 붙입니다. 예:

```env
LLM_API_KEY=여기에_키
LLM_MODEL=gpt-4o-mini
```

5. 저장합니다.

**키는 `.env`에만 넣습니다.** 채팅, 메일, README에 붙여 넣지 마세요.

Genspark로 실제 PPT를 만들려면 나중에 `GENSPARK_MODE=genspark`와 CLI 로그인이 필요합니다. 처음 실행에는 기본값(`mock`)으로 두어도 됩니다.

---

## 8. 실행하기

프로그램은 **창 두 개**가 필요합니다. 둘 다 `(lecture)` 환경이어야 합니다.

### 창 1 — 백엔드

Anaconda Prompt를 열고 아래를 그대로 붙여 넣습니다.

```bat
conda activate lecture
cd /d %USERPROFILE%\Desktop\lecture\backend
python -m uvicorn app.main:app --reload --port 8000
```

아래와 비슷한 글이 나오면 성공입니다.

```text
Uvicorn running on http://127.0.0.1:8000
```

이 창은 **닫지 마세요.**

### 창 2 — 프론트엔드

Anaconda Prompt를 **하나 더** 열고 아래를 그대로 붙여 넣습니다.

```bat
conda activate lecture
cd /d %USERPROFILE%\Desktop\lecture\frontend
npm run dev
```

아래와 비슷한 글이 나오면 성공입니다.

```text
Local: http://localhost:5173/
```

이 창도 **닫지 마세요.**

### 브라우저

Chrome 또는 Edge에서 아래 주소를 엽니다.

**http://localhost:5173**

백엔드 주소(8000)가 아니라 **5173**을 열어야 화면이 나옵니다.

---

## 디자인용 UI 미리보기 (서버 없이)

화면만 보고 싶을 때는 이 칸만 쓰면 됩니다. 폴더를 찾을 필요는 없습니다.

시작 메뉴에서 **Anaconda Prompt**를 연 뒤, 아래를 **통째로** 복사해 붙여 넣고 Enter를 누릅니다.

```bat
cd /d %USERPROFILE%\Desktop\lecture\frontend
npm install
npm run ui
```

끝나면 브라우저에서 **http://localhost:5174** 를 엽니다.

- 서버를 켜지 않아도 됩니다. API 키도 필요 없습니다.
- 위쪽 안내 띠가 보이면 미리보기 모드가 맞은 것입니다.
- **샘플 미리보기 바로 열기**를 누르면 대본이 채워진 화면으로 바로 갑니다.
- 업로드·옵션·미리보기를 눌러볼 수 있지만, PPT와 영상은 만들어지지 않고 자리표시 파일만 받습니다.

끌 때는 그 창에서 `Ctrl + C`를 누릅니다.

---

## 9. 처음 사용해 보기

1. **강의자료로 만들기** 또는 **만든 PPT로 대본·영상**을 고릅니다.
2. 파일을 고르고 업로드합니다.
3. 옵션 화면에서 대상, 강의 시간, 난이도를 고르고 **강의 프로필 저장**을 누릅니다.
4. 고급 설정에서 강의 영상 인트로를 넣을지 정할 수 있습니다.
   - 인트로 파일은 `backend\data\intros` 폴더에 두거나, 화면에서 올리면 됩니다.
5. 미리보기에서 대본을 확인합니다.
6. PPT와 강의 영상을 만듭니다. 영상은 시간이 꽤 걸릴 수 있습니다.

---

## 다음에 다시 켤 때

설치는 다시 하지 않아도 됩니다. 창 두 개만 다시 엽니다.

**창 1**

```bat
conda activate lecture
cd /d %USERPROFILE%\Desktop\lecture\backend
python -m uvicorn app.main:app --reload --port 8000
```

**창 2**

```bat
conda activate lecture
cd /d %USERPROFILE%\Desktop\lecture\frontend
npm run dev
```

브라우저에서 http://localhost:5173

끝낼 때는 두 창에서 `Ctrl + C`를 누른 뒤 창을 닫으면 됩니다.

---

## 자주 생기는 문제

**`conda`를 찾을 수 없습니다**  
시작 메뉴에서 **Anaconda Prompt**를 여세요. 일반 명령 프롬프트나 PowerShell이 아닙니다.

**`conda activate lecture` 했는데 환경 이름이 안 바뀝니다**  
먼저 `conda create -n lecture python=3.12 -y`를 한 적이 있는지 확인하세요.

**`pip` 또는 `python`을 찾을 수 없습니다**  
프롬프트 앞에 `(lecture)`가 있는지 확인하세요. 없으면 `conda activate lecture`를 다시 합니다.

**`git`을 찾을 수 없습니다**  
[Git for Windows](https://git-scm.com/download/win)를 설치하거나 `conda install git -y`를 실행한 뒤, Anaconda Prompt를 닫고 다시 여세요.

**`git clone`이 실패합니다**  
주소가 `https://github.com/ji-nyu/lecture.git`인지 확인하세요. 바탕화면에 이미 `lecture` 폴더가 있으면 다른 이름과 겹칩니다. 그 경우에는 clone을 건너뛰고 다음 단계로 가면 됩니다.

**`npm`을 찾을 수 없습니다**  
Node.js를 설치한 뒤 Anaconda Prompt를 **완전히 닫고 다시** 여세요.

**브라우저에 화면이 안 뜹니다**  
- 주소가 `http://localhost:5173`인지 확인하세요.  
- 창 2(`npm run dev`)가 켜져 있는지 확인하세요.

**업로드는 되는데 오류가 납니다 / 서버에 연결할 수 없습니다**  
창 1 백엔드가 꺼져 있는 경우가 많습니다. 백엔드를 다시 켜세요.

**포트가 이미 사용 중입니다**  
이전에 켠 창이 남아 있을 수 있습니다. 그 창에서 `Ctrl + C`로 끈 뒤 다시 실행하세요.

**강의 영상이 실패합니다**  
인터넷이 되어야 음성을 만듭니다. PowerPoint가 있으면 슬라이드 화면이 더 잘 나옵니다. 없어도 동작은 하지만, 화면이 단순해질 수 있습니다.

**대본이 어색합니다**  
`backend\.env`에 `LLM_API_KEY`와 `LLM_MODEL`을 넣고 백엔드를 **다시 시작**하세요. 설정 파일을 바꾼 뒤에는 창 1에서 `Ctrl + C` 후 실행 명령을 다시 입력합니다.

---

## 폴더가 하는 일

```text
lecture
├── backend          서버, 대본, PPT, 영상
│   ├── .env         키와 설정 (직접 만듦, 다른 곳에 올리지 말 것)
│   └── data         올린 파일, 만든 영상, 인트로
├── frontend         브라우저 화면
├── requirements.txt 파이썬 패키지 목록
└── README.md        이 파일
```

만든 강의는 `backend\data\projects`에 저장됩니다.

---

## 개발할 때

백엔드 폴더에서 테스트를 실행할 수 있습니다.

```bat
conda activate lecture
cd /d %USERPROFILE%\Desktop\lecture\backend
python -m pytest
```

설계 문서: `MASTER_SPEC.md`
