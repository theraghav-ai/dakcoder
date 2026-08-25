/**
 * A ten-line test harness, because the alternatives do not work here.
 *
 * `node:test`'s `run()` executes each file in a **child process**. Inside the
 * VS Code extension host there is no `vscode` module to import in that child and
 * no sane way to give it one, so the run simply hangs — which is how it
 * presented: seven minutes of silence and a SIGTERM, with no indication that the
 * tests had never started.
 *
 * Mocha would work, and is what the VS Code docs suggest. It is also a second
 * test framework, a dozen transitive dependencies, and a `.mocharc` in an
 * extension whose whole discipline is zero runtime dependencies — to run a
 * dozen assertions in sequence. This is that, without the dependency.
 */

type Body = () => void | Promise<void>;

const registered: { name: string; body: Body }[] = [];

export function test(name: string, body: Body): void {
  registered.push({ name, body });
}

export async function runAll(): Promise<void> {
  const failures: string[] = [];
  let passed = 0;

  for (const { name, body } of registered) {
    try {
      await body();
      passed += 1;
      console.log(`  pass  ${name}`);
    } catch (err) {
      failures.push(name);
      console.error(`  FAIL  ${name}`);
      // The assertion text is the whole value of a failure report; a stack
      // without it just says something went wrong somewhere.
      const message = err instanceof Error ? err.message : String(err);
      for (const line of message.split('\n')) console.error(`        ${line}`);
    }
  }

  console.log(`\n${passed} passed, ${failures.length} failed`);
  if (failures.length > 0) {
    throw new Error(`integration: ${failures.join(', ')}`);
  }
}
