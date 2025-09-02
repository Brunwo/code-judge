from contextlib import contextmanager
import tempfile
import shlex
from typing import Any, Generator
from app.libs.executors.executor import (
    COMPILE_ERROR_EXIT_CODE, TIMEOUT_EXIT_CODE,
    ProcessExecuteResult, ScriptExecutor, CompileError
)


RESOURCE_LIMIT_TEMPLATE = """
import java.lang.management.ManagementFactory;
import java.lang.management.MemoryMXBean;
import java.util.concurrent.TimeUnit;

public class ResourceLimit {{
    private static final long startTime = System.nanoTime();
    private static final long timeoutNanos = {timeout} * 1_000_000L; // Convert to nanoseconds
    private static final long memoryLimitBytes = {memory_limit};

    static {{
        // Set up timeout handler
        Thread timeoutThread = new Thread(() -> {{
            try {{
                Thread.sleep({timeout});
                System.err.println("Suicide from timeout.");
                System.exit({TIMEOUT_EXIT_CODE});
            }} catch (InterruptedException e) {{
                // Normal exit
            }}
        }});
        timeoutThread.setDaemon(true);
        timeoutThread.start();

        // Memory monitoring (optional - JVM handles this)
        if (memoryLimitBytes > 0) {{
            Thread memoryThread = new Thread(() -> {{
                while (true) {{
                    try {{
                        MemoryMXBean memoryBean = ManagementFactory.getMemoryMXBean();
                        long usedMemory = memoryBean.getHeapMemoryUsage().getUsed();
                        if (usedMemory > memoryLimitBytes) {{
                            System.err.println("Memory limit exceeded.");
                            System.exit({TIMEOUT_EXIT_CODE});
                        }}
                        Thread.sleep(100); // Check every 100ms
                    }} catch (Exception e) {{
                        break;
                    }}
                }}
            }});
            memoryThread.setDaemon(true);
            memoryThread.start();
        }}
    }}

    public static void checkTimeout() {{
        long currentTime = System.nanoTime();
        if (currentTime - startTime > timeoutNanos) {{
            System.err.println("Suicide from timeout.");
            System.exit({TIMEOUT_EXIT_CODE});
        }}
    }}
}}
""".strip()


class JvmExecutor(ScriptExecutor):
    def __init__(self, compiler_cl: str, run_cl: str, timeout: int = None, memory_limit: int = None):
        self.compiler_cl = compiler_cl
        self.run_cl = run_cl
        self.timeout = timeout
        self.memory_limit = memory_limit

    def setup_command(self, tmp_path: str, script: str) -> Generator[list[str], ProcessExecuteResult, None]:
        # Determine if this is Java or Kotlin based on file extension or content
        if script.strip().startswith("public class") or "class " in script[:200]:
            # Java code
            source_file = f"{tmp_path}/Main.java"
            class_name = "Main"
            is_kotlin = False
        elif "fun main" in script[:200] or "kotlin" in script[:200].lower():
            # Kotlin code
            source_file = f"{tmp_path}/Main.kt"
            class_name = "MainKt"  # Kotlin generates MainKt for files without class
            is_kotlin = True
        else:
            # Default to Java
            source_file = f"{tmp_path}/Main.java"
            class_name = "Main"
            is_kotlin = False

        # Create resource limit class for Java only if we have resource limits
        resource_file = None
        if not is_kotlin and (self.timeout or self.memory_limit):
            resource_file = f"{tmp_path}/ResourceLimit.java"
            with open(resource_file, "w") as f:
                f.write(RESOURCE_LIMIT_TEMPLATE.format(
                    timeout=self.timeout or 0,
                    memory_limit=self.memory_limit or 0,
                    TIMEOUT_EXIT_CODE=TIMEOUT_EXIT_CODE)
                )

        # Write the main source file
        with open(source_file, "w") as f:
            if not is_kotlin:
                f.write("public class Main {\n")
                f.write("    public static void main(String[] args) {\n")
                if resource_file:
                    f.write("        ResourceLimit.checkTimeout();\n")
                f.write("        try {\n")
                f.write(script)
                f.write("        } catch (Exception e) {\n")
                f.write("            e.printStackTrace();\n")
                f.write("        }\n")
                f.write("    }\n")
                f.write("}\n")
            else:
                # For Kotlin, use the script as-is but wrap in main function if needed
                if "fun main" not in script:
                    f.write("fun main() {\n")
                    f.write(script)
                    f.write("}\n")
                else:
                    f.write(script)

        # Compile step
        if is_kotlin:
            jar_file = f"{tmp_path}/Main.jar"
            compile_cmd = shlex.split(
                f"{self.compiler_cl} {source_file} -include-runtime -d {jar_file}".format(
                    source=shlex.quote(source_file),
                    workdir=shlex.quote(str(tmp_path))
                ))
        else:
            if resource_file:
                compile_cmd = shlex.split(
                    f"{self.compiler_cl} {source_file} {resource_file}".format(
                        source=shlex.quote(source_file),
                        workdir=shlex.quote(str(tmp_path))
                    ))
            else:
                compile_cmd = shlex.split(
                    f"{self.compiler_cl} {source_file}".format(
                        source=shlex.quote(source_file),
                        workdir=shlex.quote(str(tmp_path))
                    ))

        result = yield compile_cmd
        if not result.success:
            raise CompileError(result.stderr)

        # Execution step
        if is_kotlin:
            # For Kotlin, the JAR file is typically named after the source file without extension
            jar_name = source_file.replace('.kt', '.jar')
            run_cmd = shlex.split(
                f"{self.run_cl} -jar {jar_name}".format(
                    exe=shlex.quote(jar_name),
                    workdir=shlex.quote(str(tmp_path))
                ))
        else:
            run_cmd = shlex.split(
                f"{self.run_cl} -cp {tmp_path} {class_name}".format(
                    exe=shlex.quote(class_name),
                    workdir=shlex.quote(str(tmp_path))
                ))

        yield run_cmd

    def execute_script(self, script, stdin=None, timeout=None):
        try:
            return super().execute_script(script, stdin, timeout)
        except CompileError as e:
            return ProcessExecuteResult(stdout='', stderr=str(e), exit_code=COMPILE_ERROR_EXIT_CODE, cost=0)
