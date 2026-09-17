"""How slow is Qwen3-4B on CPU alone? Measured, because I have been quoting a guess.

Starts llama-server with -ngl 0 (nothing on the GPU) on a spare port, sends a prompt the size
the app actually sends, and reports the numbers that decide whether a CPU-only cloud VM is
usable for this study.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

EXE = r'D:\ChakraVue AI\TobaccoCessationBackend\bin\llamahost\llama-server.exe'
MODEL = r'D:\ChakraVue AI\TobaccoCessationBackend\models\Qwen3-4B-Q4_K_M.gguf'
PORT = 8082
BASE = 'http://127.0.0.1:%d' % PORT

# Roughly the shape ContextBuilder sends: a system block of a few hundred tokens plus a turn.
SYSTEM = (
    'You are the Quit Smoke support assistant, talking with someone who is cutting down or '
    'quitting tobacco. Tobacco is the only subject you cover. You listen, you react to what '
    'they actually said, and you find out what is going on before you suggest anything.\n'
    'HARD LIMITS - not negotiable:\n'
    '- Never diagnose, prescribe, name a medication or a dose.\n'
    '- Never claim medical certainty.\n'
    '- Never shame, blame or imply they have failed.\n'
    '- Never tell them to use, or agree that using is fine.\n'
    '- Never say you have contacted or referred anyone.\n'
    'HOW TO TALK: speak like a person, keep it short, answer what they said, do not reassure '
    'unless they are distressed.\n'
    'WHAT YOU KNOW: name Abhinay; uses cigarettes; wants to cut down; hardest time is the '
    'evening. These came from a form, not from talking to you.\n'
    'SITUATION: Acute craving. The user reports an urge happening now. Relevant context: '
    'stress, end of the working day.\n'
    'APPROVED OPTION you may offer: A minute of slower breathing - breathe in while you count '
    'to four, out while you count to six, for about a minute.\n'
) * 2

PROMPT = (
    '<|im_start|>system\n' + SYSTEM + '<|im_end|>\n'
    '<|im_start|>user\nI really want a cigarette, today has been awful<|im_end|>\n'
    '<|im_start|>assistant\n<think>\n\n</think>\n\n'
)


def wait_for_server(process, timeout=300):
    start = time.time()
    while time.time() - start < timeout:
        if process.poll() is not None:
            return False
        try:
            urllib.request.urlopen(BASE + '/health', timeout=3).read()
            return True
        except Exception:
            time.sleep(2)
    return False


def generate(n_predict, nonce=''):
    # A different prompt every run. Without this llama.cpp reuses the cached KV and reports
    # prompt_n = 1, so the measurement silently excludes prefill - which production pays on
    # every single turn, because every turn carries a different message and history.
    body = json.dumps({
        'prompt': nonce + '\n' + PROMPT,
        'n_predict': n_predict,
        'temperature': 0.75,
        'top_p': 0.8,
        'top_k': 20,
        'stop': ['<|im_end|>', '<|im_start|>'],
    }).encode()
    request = urllib.request.Request(
        BASE + '/completion', data=body,
        headers={'Content-Type': 'application/json'},
    )
    started = time.time()
    with urllib.request.urlopen(request, timeout=900) as response:
        payload = json.load(response)
    return payload, time.time() - started


def main():
    for path in (EXE, MODEL):
        if not os.path.exists(path):
            raise SystemExit('missing: ' + path)

    print('starting llama-server with -ngl 0 (CPU only, 8 threads)')
    process = subprocess.Popen(
        [EXE, '-m', MODEL, '-c', '2048', '-ngl', '0', '-t', '8',
         '--parallel', '1', '--host', '127.0.0.1', '--port', str(PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        load_started = time.time()
        if not wait_for_server(process):
            raise SystemExit('server never became healthy')
        print('  model loaded in %.0f s' % (time.time() - load_started))
        print('')

        # One warm-up so the measured runs are not paying for first-touch page faults.
        print('warm-up...')
        generate(16, nonce='warmup')

        print('%-6s %8s %10s %12s %10s' % ('run', 'tokens', 'seconds', 'tokens/sec', 'prefill_s'))
        totals = []
        for index in range(3):
            payload, seconds = generate(120, nonce='Session note %d: %s' % (index, 'the participant is new. ' * (index + 1)))
            timings = payload.get('timings', {})
            predicted = timings.get('predicted_n') or payload.get('tokens_predicted') or 0
            prompt_n = timings.get('prompt_n', 0)
            rate = predicted / seconds if seconds else 0
            prefill = timings.get('prompt_ms', 0) / 1000.0
            totals.append((seconds, rate, prompt_n, predicted, prefill))
            print('%-6d %8d %10.1f %12.2f %10.1f' % (index + 1, predicted, seconds, rate, prefill))

        avg_seconds = sum(t[0] for t in totals) / len(totals)
        avg_rate = sum(t[1] for t in totals) / len(totals)
        print('')
        print('prompt tokens      : %d  (must be ~700+, not 1)' % totals[0][2])
        print('AVERAGE prefill    : %.1f s' % (sum(t[4] for t in totals) / len(totals)))
        print('AVERAGE reply time : %.1f s' % avg_seconds)
        print('AVERAGE throughput : %.2f tokens/sec' % avg_rate)
        print('')
        print('For comparison, CLAUDE.md records 1.3 s warm on the RTX 3050.')
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except Exception:
            process.kill()
        print('server stopped')


main()
