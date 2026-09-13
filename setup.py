"""실행 환경 준비 — 새로 받은 PC에서 이것 하나만 돌리면 바로 실행 가능해진다.

    python setup.py

하는 일 (전부 멱등, 이미 끝난 단계는 건너뛴다):
    1. .venv 생성        전역 파이썬을 건드리지 않는다
    2. 패키지 설치        requirements.txt
    3. video/ 정리        mp4 하나만 두는 규칙에 맞춘다
    4. model/*.onnx      없으면 yolov8n을 받아서 export 한다
    5. ffmpeg            PATH에 없으면 .venv 안에 넣는다

venv로 격리하는 이유는 requirements.txt 에 적힌 opencv 충돌 때문이다. 전역에
opencv-python(full)이 이미 깔린 PC에서 headless를 덧설치하면 같은 cv2 경로를
덮어써서 둘 다 깨진다. 프로젝트마다 venv를 따로 두면 이 문제가 아예 생기지 않는다.

파일명이 setup.py 라서 setuptools 스크립트로 오해할 수 있지만 패키징과는 무관하다.
이 프로젝트는 배포용 패키지가 아니라 그대로 실행하는 앱이라 충돌할 일은 없다.
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
VENV = BASE / ".venv"
EXPORT_VENV = BASE / ".venv-export"     # torch를 여기 가두고 끝나면 지운다
CACHE = BASE / ".cache"                 # 받아둔 원본. 재실행 때 다시 받지 않는다
VIDEO_DIR = BASE / "video"
MODEL_DIR = BASE / "model"

# 공식 가중치. onnx 직배포본이 없어서 .pt를 받아 직접 export 한다.
PT_URL = "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.pt"
PT_NAME = "yolov8n.pt"
ONNX_NAME = "yolov8n.onnx"
# detector.py 가 입력 shape를 [1,3,H,W]로 읽으므로 dynamic 없이 640 고정으로 낸다.
# opset 12는 구형 onnxruntime(Pi의 apt 버전 포함)까지 무난히 먹는 하한선이다.
IMGSZ, OPSET = 640, 12

FFMPEG_WIN = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_LINUX = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-{arch}-static.tar.xz"

IS_WIN = os.name == "nt"
# 라즈베리파이(ARM 리눅스)에서 opencv/numpy를 pip로 빌드하면 수십 분이 걸린다.
# requirements.txt 안내대로 apt 패키지를 쓰고, venv가 그걸 볼 수 있게 만든다.
IS_ARM_LINUX = (platform.system() == "Linux"
                and platform.machine().lower().startswith(("aarch64", "armv")))


# ---------------------------------------------------------------- 유틸

class Fail(RuntimeError):
    pass


def setup_console():
    """어느 터미널에서 돌려도 한글이 깨지지 않게 출력 인코딩을 UTF-8로 통일한다.

    한국어 Windows의 콘솔 기본 코드페이지는 cp949 다. 파이썬만 UTF-8로 바꾸면
    cmd 에서 깨지고, 콘솔만 두면 UTF-8을 기대하는 Git Bash/VS Code 터미널에서
    깨진다. 코드페이지와 스트림을 둘 다 UTF-8로 맞춰야 양쪽 다 멀쩡하다.

    다운로드하는 서버 중에는 기본 User-Agent(Python-urllib)를 막는 곳이 있어
    여기서 같이 갈아둔다.
    """
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass                            # 콘솔이 아니면(파이프) 실패해도 무해하다
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    # 여기서 띄우는 자식 파이썬(검증 스니펫, export)도 같은 규칙을 따라야 한다.
    # 부모 스트림만 바꾸면 자식은 여전히 cp949로 써서 한글이 깨진다.
    os.environ["PYTHONIOENCODING"] = "utf-8"

    opener = urllib.request.build_opener()
    opener.addheaders = [("User-Agent", "safe-vibe-setup")]
    urllib.request.install_opener(opener)


def say(msg=""):
    print(msg, flush=True)


def step(n, title):
    say("\n[%d/5] %s" % (n, title))


def venv_bin(venv):
    return venv / ("Scripts" if IS_WIN else "bin")


def venv_python(venv):
    return venv_bin(venv) / ("python.exe" if IS_WIN else "python")


def run(cmd, **kw):
    """실패하면 Fail로 바꿔 던진다. 어느 명령이 죽었는지 그대로 보여준다."""
    try:
        subprocess.check_call([str(c) for c in cmd], **kw)
    except subprocess.CalledProcessError as e:
        raise Fail("명령 실패 (exit %s): %s"
                   % (e.returncode, " ".join(str(c) for c in cmd)))


def download(url, dest, label):
    """받는 도중 죽어도 반쪽 파일이 남지 않도록 .part 로 받고 마지막에 rename 한다."""
    if dest.exists() and dest.stat().st_size > 0:
        say("      %s: 이미 있음 (%.1fMB)" % (label, dest.stat().st_size / 1e6))
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    say("      %s 내려받는 중 ..." % label)

    def progress(blocks, block_size, total):
        if total <= 0:
            return
        done = min(blocks * block_size, total)
        print("\r      %6.1f / %.1fMB  %3d%%" % (done / 1e6, total / 1e6,
                                                 done * 100 // total),
              end="", flush=True)

    try:
        urllib.request.urlretrieve(url, tmp, progress)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise Fail("%s 다운로드 실패: %s\n      URL: %s" % (label, e, url))
    print()
    tmp.replace(dest)
    return dest


def single(directory, pattern, what):
    """model/ 과 video/ 의 '파일 하나만' 규칙을 setup 단계에서 미리 확인한다.

    앱이 뜬 뒤 NoModelError/NoVideoError로 죽는 것보다 여기서 잡는 편이 낫다.
    """
    found = sorted(directory.glob(pattern)) if directory.is_dir() else []
    if len(found) > 1:
        raise Fail("%s/ 에 %s가 여러 개입니다 (하나만 둬야 함): %s"
                   % (directory.name, what, ", ".join(f.name for f in found)))
    return found[0] if found else None


# ---------------------------------------------------------------- 1. venv

def step_venv(args):
    step(1, "가상환경 (.venv)")
    py = venv_python(VENV)
    if py.exists():
        say("      이미 있음: %s" % VENV)
        return py

    cmd = [sys.executable, "-m", "venv"]
    if IS_ARM_LINUX:
        # apt로 깐 python3-opencv / picamera2 / numpy 를 venv 안에서도 보이게 한다.
        # picamera2는 pip 설치가 사실상 불가능해서 이 옵션 없이는 카메라를 못 쓴다.
        cmd.append("--system-site-packages")
        say("      ARM 리눅스 감지 — system-site-packages 로 생성 (apt 패키지 재사용)")
    cmd.append(str(VENV))
    run(cmd)
    if not py.exists():
        raise Fail("venv 생성은 끝났는데 인터프리터가 없습니다: %s" % py)
    say("      생성됨: %s" % VENV)
    return py


def write_path_hook(py):
    """venv의 Scripts/bin 을 PATH 앞에 붙이는 .pth 훅.

    ffmpeg를 venv 안에 넣어두면 activate 했을 때만 PATH에 잡힌다. 그런데
    activate 없이 `.venv/Scripts/python app.py` 로 바로 실행하는 경우가 흔하고,
    그러면 frames.py 의 shutil.which("ffmpeg") 가 실패한다. site 모듈이 .pth 의
    import 로 시작하는 줄을 실행해주므로, 이 venv의 파이썬으로 실행하기만 하면
    activate 여부와 무관하게 ffmpeg를 찾게 된다.
    """
    out = subprocess.run(
        [str(py), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        capture_output=True, encoding="utf-8", errors="replace")
    site_dir = Path(out.stdout.strip())
    if not site_dir.is_dir():
        return
    (site_dir / "_safevibe_path.pth").write_text(
        "import os, sys; os.environ['PATH'] = "
        "os.path.dirname(sys.executable) + os.pathsep + os.environ.get('PATH', '')\n",
        encoding="utf-8")


# ---------------------------------------------------------------- 2. 패키지

# venv 안에서 돌려 실제로 import 되는지 본다. argv[1]은 출력 앞에 붙일 들여쓰기다.
# 하나라도 없으면 통째로 죽는 방식은 무엇이 빠졌는지 알려주지 못한다. 모듈별로
# 잡아서 버전 줄은 stdout 으로, 빠진 목록은 stderr 로 나눠 보낸다.
IMPORT_CHECK = """
import importlib, sys

missing, parts = [], []
for mod in ("cv2", "numpy", "onnxruntime"):
    try:
        m = importlib.import_module(mod)
    except Exception:
        missing.append(mod)
        parts.append(mod + " 없음")
    else:
        parts.append(mod + " " + getattr(m, "__version__", "?"))
print(sys.argv[1] + " | ".join(parts))
if missing:
    sys.stderr.write("MISSING " + " ".join(missing) + "\\n")
    sys.exit(1)
"""

MISSING_TAG = "MISSING "

# 리눅스에서 pip 로 깔면 안 되는(혹은 몇십 분 걸리는) 것들의 apt 패키지 이름.
APT_PACKAGE = {"cv2": "python3-opencv", "numpy": "python3-numpy"}

# ARM 리눅스에서 pip 로 얹는 것들. opencv/numpy 처럼 빌드가 오래 걸리는 것만
# apt 로 넘기고, 순수 파이썬 패키지는 여기서 venv 안에 깐다.
ARM_PIP_PACKAGES = ("onnxruntime", "paho-mqtt")


def check_imports(py, indent):
    """import 되는 것/안 되는 것을 한 줄로 보여주고, 빠진 모듈 목록을 돌려준다."""
    out = subprocess.run([str(py), "-c", IMPORT_CHECK, indent],
                         capture_output=True, encoding="utf-8", errors="replace")
    say(out.stdout.rstrip() or (indent + "확인 실패"))
    for line in out.stderr.splitlines():
        if line.startswith(MISSING_TAG):
            return line[len(MISSING_TAG):].split()
    return []


def missing_hints(missing):
    """빠진 모듈에 맞는 다음 행동. 원인이 다르면 처방도 달라야 한다."""
    hints = []
    pkgs = [APT_PACKAGE[m] for m in missing if m in APT_PACKAGE]
    if pkgs and IS_ARM_LINUX:
        # venv 는 --system-site-packages 로 만들었으니 apt 로 깔면 바로 보인다.
        # 여기서 .venv 를 지우라고 하면 원인과 상관없는 헛수고가 된다.
        hints.append("apt 패키지가 빠졌습니다:  sudo apt install -y %s"
                     % " ".join(pkgs))
        hints.append("깔고 나서 이 스크립트를 다시 실행하면 됩니다 (.venv 는 그대로 둬도 됩니다).")
    elif pkgs:
        hints.append("pip 설치가 덜 됐습니다. .venv 를 지우고 다시 실행해 보세요.")
    if "onnxruntime" in missing:
        hints.append("onnxruntime 설치가 실패했습니다. .venv 를 지우고 다시 실행해 보세요.")
    return hints


def step_packages(py, args):
    step(2, "패키지 설치")
    if args.skip_packages:
        say("      건너뜀 (--skip-packages)")
        return
    run([py, "-m", "pip", "install", "--upgrade", "--quiet", "pip"])

    if IS_ARM_LINUX:
        # numpy/opencv는 apt 쪽을 그대로 쓰고, 나머지 순수 파이썬 패키지만 pip로 얹는다.
        # requirements.txt 에 pip 로 깔아도 되는 게 늘면 여기에도 같이 넣어야 한다.
        say("      ARM 리눅스 — %s 만 설치합니다." % ", ".join(ARM_PIP_PACKAGES))
        say("      numpy/opencv/picamera2 는 apt 로 미리 깔아두세요:")
        say("        sudo apt install -y python3-picamera2 python3-opencv python3-numpy")
        run([py, "-m", "pip", "install", *ARM_PIP_PACKAGES])
    else:
        run([py, "-m", "pip", "install", "-r", str(BASE / "requirements.txt")])

    # 실제로 import 되는지까지 봐야 설치 성공이라 할 수 있다. 특히 opencv는
    # 설치는 되고 import 에서 깨지는 경우(전역 full 버전과 섞임)가 흔하다.
    missing = check_imports(py, "      ")
    if missing:
        raise Fail("import 실패: %s\n      %s"
                   % (", ".join(missing), "\n      ".join(missing_hints(missing))))


# ---------------------------------------------------------------- 3. 영상

def step_video(args):
    step(3, "샘플 영상 (video/)")
    VIDEO_DIR.mkdir(exist_ok=True)

    # 예전 배치대로 루트에 mp4가 있으면 규칙에 맞게 옮겨준다.
    for stray in sorted(BASE.glob("*.mp4")):
        target = VIDEO_DIR / stray.name
        if target.exists():
            say("      루트의 %s 은 video/ 에 이미 있어 건너뜁니다" % stray.name)
            continue
        shutil.move(str(stray), str(target))
        say("      %s -> video/%s 로 이동" % (stray.name, stray.name))

    video = single(VIDEO_DIR, "*.mp4", "mp4")
    if video:
        say("      video/%s (%.1fMB)" % (video.name, video.stat().st_size / 1e6))
    else:
        # 카메라(picamera2)로 돌릴 거면 영상이 없어도 되므로 실패로 보지 않는다.
        say("      ! video/ 에 mp4가 없습니다. 카메라(SOURCE=picamera)로 쓸 게 "
            "아니라면 mp4 하나를 넣어주세요.")
    return video


# ---------------------------------------------------------------- 4. 모델

EXPORT_SRC = """
import shutil, sys
from pathlib import Path
from ultralytics import YOLO

pt, out, imgsz, opset = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
produced = YOLO(pt).export(format="onnx", imgsz=imgsz, opset=opset,
                           dynamic=False, simplify=False)
out.parent.mkdir(parents=True, exist_ok=True)
shutil.move(str(produced), str(out))
print("      export 완료:", out)
"""

VERIFY_SRC = """
import ast, sys
import onnxruntime as ort

sess = ort.InferenceSession(sys.argv[1], providers=["CPUExecutionProvider"])
inp = sess.get_inputs()[0]
meta = sess.get_modelmeta().custom_metadata_map
try:
    names = ast.literal_eval(meta.get("names") or "{}")
except (ValueError, SyntaxError):
    names = {}
print("      입력 %s  클래스 %d개  task=%s" % (inp.shape, len(names), meta.get("task")))
if not names:
    sys.exit("      ! names 메타데이터가 없습니다 — detector.py 가 클래스 이름을 못 읽습니다")
if "person" not in names.values():
    print("      ! 'person' 클래스가 없습니다 — config.EVENT_CLASSES 를 확인하세요")
"""


def step_model(py, args):
    step(4, "검출 모델 (model/)")
    MODEL_DIR.mkdir(exist_ok=True)
    existing = single(MODEL_DIR, "*.onnx", "onnx")
    if existing:
        say("      이미 있음: model/%s" % existing.name)
        verify_model(py, existing)
        return existing
    if args.skip_model:
        say("      건너뜀 (--skip-model) — 모델 없이 띄우면 검출만 비활성화된다")
        return None

    pt = download(PT_URL, CACHE / PT_NAME, PT_NAME)

    # export 에는 torch가 필요한데(약 300MB) 실행에는 전혀 쓰이지 않는다.
    # 런타임 .venv 를 가볍게 유지하려고 별도 venv에 가둔 뒤 끝나면 통째로 지운다.
    say("      export 용 임시 환경을 만듭니다 (ultralytics + torch, 약 300MB 다운로드).")
    say("      실행에는 쓰이지 않고 export 가 끝나면 삭제됩니다.")
    export_py = venv_python(EXPORT_VENV)
    if not export_py.exists():
        run([sys.executable, "-m", "venv", str(EXPORT_VENV)])
    run([export_py, "-m", "pip", "install", "--upgrade", "--quiet", "pip"])
    run([export_py, "-m", "pip", "install", "ultralytics", "onnx"])

    target = MODEL_DIR / ONNX_NAME
    # ultralytics는 실행 디렉터리에 캐시/로그를 흘리므로 .cache 안에서 돌린다.
    env = dict(os.environ, YOLO_VERBOSE="False")
    try:
        run([export_py, "-c", EXPORT_SRC, pt, target, IMGSZ, OPSET],
            cwd=str(CACHE), env=env)
    finally:
        if not args.keep_export_env and EXPORT_VENV.exists():
            say("      임시 export 환경 삭제 중 ...")
            shutil.rmtree(EXPORT_VENV, ignore_errors=True)

    if not target.exists():
        raise Fail("export 는 끝났는데 onnx 파일이 없습니다")
    say("      model/%s (%.1fMB)" % (target.name, target.stat().st_size / 1e6))
    verify_model(py, target)
    return target


def verify_model(py, onnx):
    """detector.py 가 실제로 읽는 항목(입력 shape, names 메타데이터)을 그대로 확인한다."""
    try:
        run([py, "-c", VERIFY_SRC, onnx])
    except Fail as e:
        say("      ! 모델 검증 실패: %s" % e)


# ---------------------------------------------------------------- 5. ffmpeg

def find_ffmpeg():
    """PATH 와 venv 안쪽을 모두 본다. venv 쪽은 activate 전이면 PATH에 없다."""
    return shutil.which("ffmpeg") or shutil.which("ffmpeg", path=str(venv_bin(VENV)))


def step_ffmpeg(args):
    step(5, "ffmpeg")
    if args.skip_ffmpeg:
        say("      건너뜀 (--skip-ffmpeg)")
        return None
    found = find_ffmpeg()
    if found:
        say("      이미 있음: %s" % found)
        return found
    if IS_ARM_LINUX:
        # Pi는 카메라 입력이 기본이라 ffmpeg가 필요 없다. 영상 파일을 쓸 때만 필요.
        say("      없음 — 카메라(picamera2)로 쓸 거면 필요 없습니다.")
        say("      영상 파일로 쓸 거면: sudo apt install -y ffmpeg")
        return None
    if IS_WIN:
        return install_ffmpeg_windows()
    if platform.system() == "Linux":
        return install_ffmpeg_linux()
    say("      자동 설치를 지원하지 않는 OS 입니다. 직접 설치하세요:  brew install ffmpeg")
    return None


def install_ffmpeg_windows():
    """정적 빌드에서 exe 두 개만 꺼내 venv Scripts 에 둔다.

    시스템 PATH나 레지스트리를 건드리지 않으므로, 프로젝트를 지우면 같이 사라진다.
    """
    zip_path = download(FFMPEG_WIN, CACHE / "ffmpeg-win64.zip", "ffmpeg (Windows)")
    dest = venv_bin(VENV)
    dest.mkdir(parents=True, exist_ok=True)
    wanted = ("ffmpeg.exe", "ffprobe.exe")
    got = []
    with zipfile.ZipFile(zip_path) as z:
        for member in z.namelist():
            name = Path(member).name
            if name in wanted and "/bin/" in member.replace("\\", "/"):
                with z.open(member) as src, open(dest / name, "wb") as out:
                    shutil.copyfileobj(src, out)
                got.append(name)
    if "ffmpeg.exe" not in got:
        raise Fail("zip 안에서 ffmpeg.exe 를 찾지 못했습니다")
    say("      설치됨: %s" % (dest / "ffmpeg.exe"))
    return dest / "ffmpeg.exe"


def install_ffmpeg_linux():
    arch = {"x86_64": "amd64", "i686": "i686"}.get(platform.machine(), "amd64")
    tar_path = download(FFMPEG_LINUX.format(arch=arch),
                        CACHE / ("ffmpeg-%s.tar.xz" % arch), "ffmpeg (Linux)")
    dest = venv_bin(VENV)
    dest.mkdir(parents=True, exist_ok=True)
    found = False
    with tarfile.open(tar_path) as t:
        for member in t.getmembers():
            if member.isfile() and Path(member.name).name in ("ffmpeg", "ffprobe"):
                out = dest / Path(member.name).name
                with t.extractfile(member) as src, open(out, "wb") as f:
                    shutil.copyfileobj(src, f)
                out.chmod(0o755)
                found = found or out.name == "ffmpeg"
    if not found:
        raise Fail("tar 안에서 ffmpeg 를 찾지 못했습니다")
    say("      설치됨: %s" % (dest / "ffmpeg"))
    return dest / "ffmpeg"


# ---------------------------------------------------------------- 점검 / 요약

# picamera2 는 apt 로 깔리므로 이 스크립트를 돌리는 파이썬에서는 안 보일 수 있다.
# 앱과 같은 조건으로 보려면 .venv 의 파이썬에서 물어봐야 한다.
CAMERA_CHECK = """
try:
    from picamera2 import Picamera2
except ImportError:
    print("picamera2 없음 (SOURCE=auto 는 영상 파일을 쓴다)")
else:
    try:
        info = Picamera2.global_camera_info()
    except Exception as e:
        print("picamera2 는 있으나 조회 실패: %s" % e)
    else:
        print("%d대 연결됨" % len(info) if info else
              "picamera2 는 있으나 연결된 카메라 없음 (SOURCE=video 로 실행)")
"""


def camera_status(py):
    """카메라 연결 여부. 모듈 설치와 실제 연결은 별개라 둘 다 구분해서 보여준다."""
    if not py.exists():
        return "확인 불가 (.venv 없음)"
    out = subprocess.run([str(py), "-c", CAMERA_CHECK],
                         capture_output=True, encoding="utf-8", errors="replace")
    return out.stdout.strip() or (out.stderr.strip().splitlines() or ["확인 실패"])[-1]


def do_check():
    say("환경 점검\n")
    py = venv_python(VENV)
    say("  .venv        : %s" % (py if py.exists() else "없음"))
    if py.exists():
        for hint in missing_hints(check_imports(py, "  패키지       : ")):
            say("                 %s" % hint)
    # 점검은 무슨 상태든 끝까지 보여줘야 쓸모가 있다. 규칙 위반(파일 여러 개)도
    # 예외로 죽이지 말고 그 줄에 이유를 적는다.
    for label, directory, pattern, what in (
            ("video/", VIDEO_DIR, "*.mp4", "mp4"),
            ("model/", MODEL_DIR, "*.onnx", "onnx")):
        try:
            found = single(directory, pattern, what)
            say("  %-12s : %s" % (label, label + found.name if found else "없음"))
        except Fail as e:
            say("  %-12s : %s" % (label, e))
    say("  %-12s : %s" % ("ffmpeg", find_ffmpeg() or "없음"))
    # 한글 라벨은 %-12s 로 맞추면 폭이 어긋난다. 위 '패키지' 줄과 같은 방식으로 직접 띄운다.
    say("  카메라       : %s" % camera_status(py))


def summary():
    say("\n" + "-" * 62)
    say("준비 완료. 실행:")
    if IS_WIN:
        # Git Bash 에서는 백슬래시가 이스케이프로 먹혀서 경로가 통째로 깨진다
        # (.venv\Scripts\python -> .venvScriptspython). 셸을 알 수 없으니 둘 다 적는다.
        say("    PowerShell / cmd :  .venv\\Scripts\\python app.py")
        say("    Git Bash         :  .venv/Scripts/python app.py")
        say("")
        say("  또는 활성화한 뒤 `python app.py`:")
        say("    PowerShell       :  .venv\\Scripts\\Activate.ps1")
        say("    cmd              :  .venv\\Scripts\\activate.bat")
        say("    Git Bash         :  source .venv/Scripts/activate")
    else:
        say("    .venv/bin/python app.py")
        say("")
        say("  또는 활성화한 뒤 `python app.py`:")
        say("    source .venv/bin/activate")
    say("")
    say("  브라우저에서  http://localhost:8080")
    say("  설정은 환경변수로 덮어쓴다:  STREAM_W=640 AI_FPS=2 python app.py")
    say("-" * 62)


def main():
    ap = argparse.ArgumentParser(description="safe-vibe 실행 환경 준비")
    ap.add_argument("--check", action="store_true", help="상태만 점검하고 끝낸다")
    ap.add_argument("--skip-packages", action="store_true")
    ap.add_argument("--skip-model", action="store_true",
                    help="모델 다운로드/export 생략 (검출 없이 스트리밍만)")
    ap.add_argument("--skip-ffmpeg", action="store_true")
    ap.add_argument("--keep-export-env", action="store_true",
                    help="export용 .venv-export 를 지우지 않는다 (모델 재변환에 유용)")
    args = ap.parse_args()
    setup_console()

    if args.check:
        do_check()
        return 0

    say("safe-vibe 환경 준비  (%s %s, Python %s)"
        % (platform.system(), platform.machine(), platform.python_version()))
    CACHE.mkdir(exist_ok=True)
    try:
        py = step_venv(args)
        step_packages(py, args)
        write_path_hook(py)
        step_video(args)
        step_model(py, args)
        step_ffmpeg(args)
    except Fail as e:
        say("\n실패: %s" % e)
        return 1
    except KeyboardInterrupt:
        say("\n중단됨. 다시 실행하면 끝난 단계는 건너뜁니다.")
        return 130
    summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
