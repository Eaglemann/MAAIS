import { useState } from "react";

export function Login({
  busy,
  error,
  reason,
  onSubmit,
}: {
  busy: boolean;
  error: string | null;
  reason: "required" | "expired" | "signed_out";
  onSubmit: (password: string) => Promise<void>;
}) {
  const [password, setPassword] = useState("");

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!password || busy) return;
    await onSubmit(password);
    setPassword("");
  }

  const message = reason === "expired"
    ? "Your operator session expired. Sign in again to continue."
    : reason === "signed_out"
      ? "You are signed out. All trading evidence remains stored on the server."
      : "Use the sole-operator passphrase to access paper-trading evidence and controls.";

  return (
    <main className="login-shell">
      <section className="login-card" aria-labelledby="login-title">
        <div className="brand-lockup login-brand">
          <span className="brand-mark">M</span>
          <div><strong>MAAIS</strong><span>Paper workstation</span></div>
        </div>
        <span className="kicker">Private Mission Control</span>
        <h1 id="login-title">Sign in to Mission Control</h1>
        <p>{message}</p>
        <form onSubmit={(event) => void submit(event)}>
          <label>
            <span>Operator passphrase</span>
            <input
              aria-label="Operator passphrase"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              disabled={busy}
              autoFocus
            />
          </label>
          <button type="submit" disabled={!password || busy}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
        {error && <div className="error-panel" role="alert">{error}</div>}
        <div className="login-boundary">
          <strong>Paper trading only</strong>
          <span>No live-money adapter or exchange key is used.</span>
        </div>
      </section>
    </main>
  );
}
