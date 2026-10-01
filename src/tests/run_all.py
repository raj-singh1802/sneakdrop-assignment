import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).parent
SCRIPTS = ["load_test.py", "payment_test.py", "soak_test.py"]

failed = []
for script in SCRIPTS:
    print(f"\n===== {script} =====", flush=True)
    if subprocess.call([sys.executable, str(HERE / script)]) != 0:
        failed.append(script)

print("\nALL SUITES PASSED" if not failed else f"\nFAILED: {', '.join(failed)}")
sys.exit(1 if failed else 0)
