export async function register() {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;

  const { ensureScheduler } = await import("./lib/scheduler");
  ensureScheduler();
}
