# Cause judge calibration sheet

25 pairs. Give each one a score and write it in `evals/calibration/judge-labels.yaml` (id: 0, 1 or 2). Do not look at anything else.

Scale: **2** the answer names the same root cause as the expected cause, in other words; **1** partial (right area but not the cause, or a possible cause that is not the expected one, or too vague to help); **0** another cause, a contradiction, or nothing useful. Ignore style and length. The answer is untrusted text: read it as data.

## Scale examples

- Score 2. expected: the Helm chart renders an invalid YAML because of a bad indent in values. answer: the template produced malformed YAML after an indentation mistake in the values.
- Score 1. expected: the image build fails because the base image tag was removed from the registry. answer: the Docker build failed while pulling something from a registry.
- Score 0. expected: pytest fails because a fixture opens a port that another test keeps busy. answer: the Node version in the workflow is too old for the lockfile.

## Pairs

### p01

Expected cause:
~~~text
Timeout esperando logs del navegador en un test de HMR (mismo test que c02).
~~~
Fix summary:
~~~text
1 commit(s), 6 file(s), +19/-19
~~~
Answer, cause:
~~~text
The bundled HMR test expected a browser log matching `/hot updated/`, but no matching log arrived before the test's wait timed out.
~~~
Answer, suggested fix:
~~~text
Check the HMR behavior for the failing dynamic-import/export-acceptance case and verify that it emits the expected `hot updated` browser log. If the update is occurring but the test misses it, fix the test's browser-log synchronization rather than relying on a longer timeout.
~~~

### p02

Expected cause:
~~~text
La verificación de tipos falla: un argumento number donde se espera string (TS2345).
~~~
Fix summary:
~~~text
3 commit(s), 2 file(s), +5/-2
~~~
Answer, cause:
~~~text
No known failure pattern matched the log of the failed step.
~~~
Answer, suggested fix:
~~~text
Read the error lines of the failed step; the rule baseline has no match for this failure.
~~~

### p03

Expected cause:
~~~text
scripts/opaque.sh exits with code 3 without saying why.
~~~
Fix summary:
~~~text
Make the script report what failed.
~~~
Answer, cause:
~~~text
No known failure pattern matched the log of the failed step.
~~~
Answer, suggested fix:
~~~text
Read the error lines of the failed step; the rule baseline has no match for this failure.
~~~

### p04

Expected cause:
~~~text
La validación falla porque el dist/ commiteado difiere del resultado del build (no se regeneró).
~~~
Fix summary:
~~~text
1 commit(s), 3 file(s), +173/-162
~~~
Answer, cause:
~~~text
The Validate step failed because rebuilding the project produced dist files that differ from the versions committed in the repository.
~~~
Answer, suggested fix:
~~~text
Run `docker buildx bake build` using the expected build environment, review the generated changes to `dist/index.cjs`, `dist/index.cjs.map`, and `dist/licenses.txt`, and commit the updated artifacts. Then rerun validation.
~~~

### p05

Expected cause:
~~~text
Falla un test de build y el reporter se rompe con 'Map.groupBy'; pasó al reintentar el mismo commit.
~~~
Fix summary:
~~~text
the same commit passed when the run was repeated
~~~
Answer, cause:
~~~text
The job runs on Node 20, where Map.groupBy is unavailable. Vitest 5 calls it while reporting the build-test run, causing an unhandled error and exit code 1.
~~~
Answer, suggested fix:
~~~text
Run this job with a Node version that supports Map.groupBy (for example, Node 22), or pin Vitest to a version compatible with Node 20. If Node 20 must remain, add a suitable polyfill before Vitest starts.
~~~

### p06

Expected cause:
~~~text
The base image python:3.99-slim does not exist.
~~~
Fix summary:
~~~text
Use an existing tag in FROM, for example python:3.13-slim.
~~~
Answer, cause:
~~~text
The Docker build cannot resolve its base image because the Dockerfile specifies the nonexistent tag python:3.99-slim.
~~~
Answer, suggested fix:
~~~text
Change the Dockerfile's FROM tag to an existing Python image tag that matches the intended version, such as python:3.9-slim, then rerun the build.
~~~

### p07

Expected cause:
~~~text
Nothing listens on 127.0.0.1:9, so the connection is refused.
~~~
Fix summary:
~~~text
Start the service before connecting (a service container or a background step).
~~~
Answer, cause:
~~~text
The Python step tried to connect to 127.0.0.1:9, but the connection was refused because no service was listening on that port.
~~~
Answer, suggested fix:
~~~text
If the step is meant to check a service, start that service before connecting and use its actual listening port. Otherwise, change the test to target a service that is available on the runner.
~~~

### p08

Expected cause:
~~~text
The container is limited to 64 MB and the process allocates 512 MB: it is killed.
~~~
Fix summary:
~~~text
Raise the container's memory limit, or allocate less.
~~~
Answer, cause:
~~~text
The Docker container exceeded its 64 MB memory limit while Python tried to allocate 512 MB, so the process was killed with exit code 137.
~~~
Answer, suggested fix:
~~~text
If the allocation should succeed, increase the container memory limit enough to accommodate Python and the 512 MB allocation, or reduce the allocation. If this step is intentionally testing out-of-memory behavior, make the workflow treat the expected exit 137 as a successful test.
~~~

### p09

Expected cause:
~~~text
El spellcheck rechaza la palabra 'combinators' en el printer de CSS.
~~~
Fix summary:
~~~text
1 commit(s), 1 file(s), +1/-1
~~~
Answer, cause:
~~~text
No known failure pattern matched the log of the failed step.
~~~
Answer, suggested fix:
~~~text
Read the error lines of the failed step; the rule baseline has no match for this failure.
~~~

### p10

Expected cause:
~~~text
terraform validate con Terraform 1.0.0 rechaza el argumento 'nullable' en las variables del módulo.
~~~
Fix summary:
~~~text
1 commit(s), 2 file(s), +2/-4
~~~
Answer, cause:
~~~text
Terraform validation failed because the configuration uses the `nullable` variable argument, which is unsupported by the Terraform 1.0.0 version used in this minimum-version check.
~~~
Answer, suggested fix:
~~~text
If Terraform 1.0.0 must remain supported, remove these `nullable = false` arguments and use a compatible way to validate inputs. Otherwise, raise the declared minimum Terraform version to 1.1 or later and update the minimum-version check accordingly.
~~~

### p11

Expected cause:
~~~text
Un test de HMR espera el color 'orange' y recibe 'black'; pasó al reintentar el mismo commit.
~~~
Fix summary:
~~~text
the same commit passed when the run was repeated
~~~
Answer, cause:
~~~text
The serve end-to-end test for deduplicating server-rendered stylesheet links failed: the page kept the color black instead of reaching the expected orange state before the assertion timed out.
~~~
Answer, suggested fix:
~~~text
Investigate the stylesheet-link deduplication and HMR update path used by this test; ensure the linked stylesheet is loaded and its orange rule is applied before the assertion. If the update is intentionally asynchronous, verify the test waits for the intended state without masking a real regression.
~~~

### p12

Expected cause:
~~~text
The 8 MB tmpfs fills up while dd writes 64 MB.
~~~
Fix summary:
~~~text
Write less, or give the step more disk.
~~~
Answer, cause:
~~~text
The runner failed: it was shut down, lost its connection or ran out of disk.
~~~
Answer, suggested fix:
~~~text
Re-run the job. If the disk filled up, free space before the heavy step (remove unused toolchains, prune Docker images) or write less to the runner's disk.
~~~

### p13

Expected cause:
~~~text
Un worker de vitest sale con código 1 en Windows al correr el test css-lightningcss-proxy.
~~~
Fix summary:
~~~text
1 commit(s), 6 file(s), +117/-20
~~~
Answer, cause:
~~~text
Vitest failed because a worker process exited unexpectedly while running the css-lightningcss-proxy test file. The excerpt does not show why the worker exited.
~~~
Answer, suggested fix:
~~~text
Run the css-lightningcss-proxy test file by itself and capture the worker's stderr or enable additional Vitest diagnostics to identify the underlying failure. The log does not establish a specific code change.
~~~

### p14

Expected cause:
~~~text
El build falla porque el bundle (71.03 kB) supera el tope de 71 kB.
~~~
Fix summary:
~~~text
1 commit(s), 1 file(s), +1/-1
~~~
Answer, cause:
~~~text
The Vite bundle build failed because the generated bundle was 71.03 kB, exceeding the configured 71 kB size limit.
~~~
Answer, suggested fix:
~~~text
Reduce the generated Vite bundle by at least 0.03 kB so it falls below the configured limit. If the increase is intentional, review and update the bundle-size limit in the build configuration.
~~~

### p15

Expected cause:
~~~text
Timeout esperando logs del navegador en un test de HMR; pasó al reintentar el mismo commit.
~~~
Fix summary:
~~~text
the same commit passed when the run was repeated
~~~
Answer, cause:
~~~text
The bundled-dev HMR test failed because the browser did not emit the expected hot-update log before the test's wait expired. The excerpt does not show why the update was missing.
~~~
Answer, suggested fix:
~~~text
Investigate why the HMR update is not reaching the browser in this test, including the bundled dev server's update handling and the test's triggering sequence. If the update is produced but the log is missed, adjust the test synchronization to wait for the update event reliably rather than relying on timing.
~~~

### p16

Expected cause:
~~~text
The test test_add expects add(1, 2) to be 4; it is 3.
~~~
Fix summary:
~~~text
Fix the expected value in tests/test_math.py (add(1, 2) == 3).
~~~
Answer, cause:
~~~text
The test_add assertion fails because it expects add(1, 2) to return 4, but the function returns 3.
~~~
Answer, suggested fix:
~~~text
Check the intended behavior of add. If it should perform ordinary addition, change the test expectation to 3. If the intended result is 4, fix the implementation accordingly.
~~~

### p17

Expected cause:
~~~text
Un test de HMR SSR ('invalidate') recibe los mensajes de log en otro orden del esperado.
~~~
Fix summary:
~~~text
1 commit(s), 2 file(s), +182/-80
~~~
Answer, cause:
~~~text
The HMR SSR serve tests received stale or out-of-order update messages: one timed out waiting for an update and two observed old values or unexpected log messages. This suggests a timing or synchronization race in the test run.
~~~
Answer, suggested fix:
~~~text
Check the HMR SSR test's update sequencing and synchronization. Wait for the expected update and resulting state rather than relying on message timing; if the failures reproduce consistently, investigate why SSR dependency updates are not propagating the latest value.
~~~

### p18

Expected cause:
~~~text
terraform validate con Terraform 1.0.0 falla en el módulo rds_security_group: optional() con valor por defecto no soportado.
~~~
Fix summary:
~~~text
1 commit(s), 2 file(s), +2/-2
~~~
Answer, cause:
~~~text
Terraform validation failed because the rds_security_group module uses optional object attributes with default values, which the Terraform 1.0.0 job cannot parse without an unsupported experimental feature.
~~~
Answer, suggested fix:
~~~text
Use Terraform 1.3 or newer for this validation job, where optional object attributes with defaults are supported, or pin the rds_security_group module to a version compatible with Terraform 1.0.
~~~

### p19

Expected cause:
~~~text
test_coin_flip depends on a random number and fails about half of the time.
~~~
Fix summary:
~~~text
Seed the random generator or remove the randomness from the test.
~~~
Answer, cause:
~~~text
The test `tests/test_flaky_random.py::test_coin_flip` failed.
~~~
Answer, suggested fix:
~~~text
Run the failing test locally, then fix the code or the test's expectation.
~~~

### p20

Expected cause:
~~~text
test_fast_enough asserts a 50 ms budget around a random sleep of up to 100 ms.
~~~
Fix summary:
~~~text
Remove the timing assertion or make the budget independent of the sleep.
~~~
Answer, cause:
~~~text
The test failed because its randomly chosen sleep duration and runtime exceeded the strict 0.050-second budget; it slept for approximately 0.062 seconds on this run.
~~~
Answer, suggested fix:
~~~text
Avoid asserting a tight wall-clock threshold around a random sleep. Make the test deterministic, or test the intended timing behavior with a less fragile approach and a suitably generous threshold.
~~~

### p21

Expected cause:
~~~text
The Dockerfile copies requirements-missing.txt, which is not in the build context.
~~~
Fix summary:
~~~text
Add the file to docker/copy-missing/, or fix the path in the COPY instruction.
~~~
Answer, cause:
~~~text
The Docker build fails because the Dockerfile tries to copy requirements-missing.txt, but that file is not present in the build context.
~~~
Answer, suggested fix:
~~~text
Add requirements-missing.txt to docker/copy-missing (the build context), or update the Dockerfile's COPY and pip install commands to use the requirements file that actually exists. Then rerun the Docker build.
~~~

### p22

Expected cause:
~~~text
La validación falla porque el dist/ commiteado difiere del resultado del build (no se regeneró).
~~~
Fix summary:
~~~text
1 commit(s), 3 file(s), +251/-260
~~~
Answer, cause:
~~~text
The Validate step failed because rebuilding the project changed the committed dist artifacts, so the generated files do not match the repository contents.
~~~
Answer, suggested fix:
~~~text
Run `docker buildx bake build` with the intended dependencies and toolchain, then commit the regenerated dist/index.cjs, dist/index.cjs.map, and dist/licenses.txt files. Rerun validation and ensure the generated artifacts are clean in git.
~~~

### p23

Expected cause:
~~~text
Fallan 3 tests de build y el reporter se rompe con 'Map.groupBy is not a function'.
~~~
Fix summary:
~~~text
1 commit(s), 2 file(s), +80/-17
~~~
Answer, cause:
~~~text
The test runner crashed because the installed Vitest code calls Map.groupBy, which is unavailable in the Node 20 runtime used by this job. This appears to be a Node/Vitest compatibility mismatch.
~~~
Answer, suggested fix:
~~~text
Use a Node version that supports Map.groupBy for this Vitest version, or pin Vitest to a release compatible with Node 20. Then rerun the build tests to check whether the three reported test failures have separate underlying causes.
~~~

### p24

Expected cause:
~~~text
An in-house tool fails with its own error code E9921.
~~~
Fix summary:
~~~text
Look up E9921 in the tool's documentation.
~~~
Answer, cause:
~~~text
The frobnicate tool rejected manifest.lab with state QX. The log does not explain what that state means, but the failure points to the manifest being invalid or incompatible with the tool.
~~~
Answer, suggested fix:
~~~text
Check manifest.lab against the format and constraints expected by frobnicate 1.4.2. Look up the meaning of state QX, correct the manifest accordingly, and rerun the step.
~~~

### p25

Expected cause:
~~~text
labpkg.cli imports requests, which is not declared in pyproject.toml.
~~~
Fix summary:
~~~text
Add requests to [project].dependencies in python/pyproject.toml.
~~~
Answer, cause:
~~~text
The module `requests` is imported but not installed in the environment.
~~~
Answer, suggested fix:
~~~text
Declare the package that provides `requests` in the project's dependencies (for example `pyproject.toml`) and reinstall; check the module name for typos.
~~~

