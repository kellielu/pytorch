import collections
import json
import multiprocessing
import multiprocessing.dummy
import os
import queue
import pickle
import re
import statistics
import subprocess
import sys
import textwrap
import threading
import traceback
import time


ROOT = os.path.dirname(os.path.abspath(__file__))
OP_BENCHMARK_ROOT = os.path.split(os.path.split(ROOT)[0])[0]

HEAD = "head"
VERSIONS = (HEAD, "1.6", "1.5", "1.4", "1.3")
ENV_TEMPLATE = "historic_microbenchmark_{version}"

EXCLUDE = [
    "channel_shuffle_test.py",
    "hardsigmoid_test.py",
    "hardswish_test.py",
    "q.+_test.py",
]

Task = collections.namedtuple("Task", ("version", "test", "num_cores", "device", "tag_filter"))

CPU_QUEUE = queue.Queue()
for i in range(0, multiprocessing.cpu_count() - 3, 2):
    CPU_QUEUE.put(i)

GPU_QUEUE = queue.Queue()
GPU_QUEUE.put(0)
GPU_QUEUE.put(1)

RESULT_QUEUE = queue.Queue()


def make_env(version):
    assert version in VERSIONS
    env_name = ENV_TEMPLATE.format(version=version)

    nvcc_install = "conda install -y -c nvidia nvcc_linux-64"
    cmd = textwrap.dedent(f"""
        conda env remove --name {env_name} 2> /dev/null || true
        conda create --no-default-packages -yn {env_name} python=3
        source activate {env_name}
        conda install -y numpy ninja pyyaml mkl mkl-include setuptools cmake cffi hypothesis
        conda install -y -c pytorch magma-cuda102
        {nvcc_install if version in ('1.3', '1.4') else ''}
    """).strip().replace("\n", " && ")

    print(f"Making clean env: {env_name}")
    result = subprocess.run(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert not result.returncode

    if version == HEAD:
        cmd = (
            f"cd {ROOT} && cd $(git rev-parse --show-toplevel) "
            f"&& source activate {env_name} && python setup.py clean && "
            "python setup.py install"
        )
        print("Building PyTorch:")
        result = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert not result.returncode
    else:
        print(f"Installing pytorch=={version} and patching benchmark utilities.")
        cmd = (
            f"source activate {env_name} && conda install -y -c pytorch pytorch=={version} && "
            f"cd {OP_BENCHMARK_ROOT}/pt_extension && python setup.py install &&"
            "cp -r $(git rev-parse --show-toplevel)/torch/utils/_benchmark "
            "$(python -c 'import torch;import os;print(os.path.dirname(os.path.abspath(torch.__file__)))')/utils/"
        )
        result = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert not result.returncode


def count_benchmarks(test: str):
    cmd = (
        f"cd {OP_BENCHMARK_ROOT} && "
        f"source activate {ENV_TEMPLATE.format(version=HEAD)} && "
        f"python -m {test} --list_tests"
    )
    result = subprocess.run(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "CUDA_VISIBLE_DEVICES": "",
            "PATH": os.getenv("PATH"),
        },
    )
    assert not result.returncode
    count, active = 0, False
    for l in result.stdout.decode("utf-8").splitlines():
        l = l.strip()
        if l.startswith("# List of tests:"):
            active = True
            continue

        if not l:
            active = False
            continue

        count += 1
    return count


def launch_subtask(t: Task):
    cpu = None
    gpu = None
    try:
        cpu = CPU_QUEUE.get()
        if t.device == "cuda":
            gpu = GPU_QUEUE.get()

        cpu_list = str(cpu) if t.num_cores == 1 else f"{cpu}-{cpu + t.num_cores - 1}"
        cmd = (
            f"cd {OP_BENCHMARK_ROOT} && "
            f"source activate {ENV_TEMPLATE.format(version=t.version)} && "
            f"taskset --cpu-list {cpu_list} "
            f"python -m {t.test} "
            f"--tag_filter {t.tag_filter} --ai_pep_format --device {t.device} "
            f"--omp_num_threads {t.num_cores} --mkl_num_threads  {t.num_cores}"
        )
        result = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                "CUDA_VISIBLE_DEVICES": "" if gpu is None else str(gpu),
                "PATH": os.getenv("PATH"),
            },
        )
        if not result.returncode:
            RESULT_QUEUE.put((t, result.stdout.decode("utf-8")))
        else:
            RESULT_QUEUE.put((None, None))
            stdout = result.stdout.decode("utf-8")
            stderr = result.stderr.decode("utf-8")
            def condense(s: str):
                lines = s.splitlines()
                if len(lines) > 20:
                    lines = lines[:10] + ["..."] + lines[-10:]
                return "\n".join(lines)
            print(
                f"Run failed: {t}\n"
                f"Return code: {result.returncode}\n"
                f"stdout:\n{condense(stdout)}\n"
                f"stderr:\n{condense(stderr)}")

    except KeyboardInterrupt:
        pass

    finally:
        if cpu is not None:
            CPU_QUEUE.put(cpu)
        if gpu is not None:
            GPU_QUEUE.put(gpu)


def parse_output():
    t, stdout = RESULT_QUEUE.get(timeout=3600)
    if t is None:
        return None, None
    results = []
    for l in stdout.splitlines():
        if l.startswith("# Benchmarking PyTorch"):
            results.append([])
            continue

        if l.startswith("PyTorchObserver "):
            l = l[len("PyTorchObserver "):]
            data = json.loads(l.strip())
            results[-1].append(data)

    if not results:
        print("Failed to extract data:")
        print(t)
        print(stdout)

    return t, results


def process(results):
    structured_results = collections.defaultdict(list)
    for t, r in results:
        if t is None:
            continue

        for ri in r:
            types = {i["type"] for i in ri}
            assert len(types) == 1
            run_type = types.pop()

            assert all(i["metric"] == "latency" for i in ri)

            times = tuple(float(j["value"]) * {"ms": 1e-3}[j["unit"]] for j in ri)
            key = (t.num_cores, t.device, run_type)
            structured_results[key].append((t.version, times))

    sorted_results = {}
    for k in sorted(structured_results.keys()):
        v = sorted(structured_results[k])
        sorted_results[k] = v

    return sorted_results


def run():
    tests = []
    for i in sorted(os.listdir(os.path.join(OP_BENCHMARK_ROOT, "pt"))):
        if not i.endswith("_test.py"):
            continue

        if any(re.search(pattern, i) for pattern in EXCLUDE):
            continue
        tests.append(f"pt.{i[:-3]}")

    # By pre-sorting based on the number of test cases, we can prevent
    # stragglers and reduce the overall test time.
    print("Sorting tests by the number of cases.")
    tests.sort(key=count_benchmarks, reverse=True)

    cpu_tasks, gpu_tasks = [], []
    for test in tests:
        for v in VERSIONS:
            cpu_tasks.extend([
                Task(v, test, 1, "cpu", "short"),
                Task(v, test, 1, "cpu", "long"),
                Task(v, test, 2, "cpu", "short"),
                Task(v, test, 2, "cpu", "long"),
            ])
            gpu_tasks.append(Task(v, test, 2, "cuda", "all"))

    print("Beginning run:")
    gpu_pool = multiprocessing.dummy.Pool(GPU_QUEUE.qsize())
    cpu_pool = multiprocessing.dummy.Pool(CPU_QUEUE.qsize())

    gpu_work = gpu_pool.map_async(launch_subtask, gpu_tasks, 1)
    time.sleep(0.5)
    cpu_work = cpu_pool.map_async(launch_subtask, cpu_tasks, 1)

    results = []
    def snapshot():
        print("\nSnapshotting results.")
        parsed_results = process(results)
        with open("/tmp/microbenchmarks_parsed.pkl", "wb") as f:
            pickle.dump(parsed_results, f)

    with open("/tmp/microbenchmarks.pkl", "wb") as f:
        pass

    n_tasks = len(cpu_tasks) + len(gpu_tasks)
    for i in range(1, n_tasks + 1):
        ri = parse_output()
        with open("/tmp/microbenchmarks.pkl", "ab") as f:
            pickle.dump(ri, f)
        results.append(ri)
        print(f"\r{i} / {n_tasks}", end="")

        if not (i % int(n_tasks / 10)):
            snapshot()

    print("")
    snapshot()


def main():
    # for v in VERSIONS:
    #     make_env(v)
    run()


if __name__ == "__main__":
    main()
