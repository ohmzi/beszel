import { Trans } from "@lingui/react/macro"
import { memo, useEffect, useState } from "react"
import { Logo } from "@/components/logo"
import { pb } from "@/lib/api"

// Ohmz fork: the alert acknowledgement landing page the e-mail links to, now served by the NEW site.
// The token in the link was minted by the hub (see the /maintenance/ack/mint route); this page only
// reads the query string, asks the hub to describe the token, and on confirm asks the hub to apply it.
// It works without logging in, because the token is the credential, and a GET never acknowledges.
type Peek = { valid?: boolean; fp?: string; sev?: string; days?: number; exp?: number; title?: string }

export default memo(() => {
	const [q] = useState(() => new URLSearchParams(window.location.search))
	const token = q.get("t") ?? ""
	const sev = q.get("s") ?? "warn"
	const [peek, setPeek] = useState<Peek | null | undefined>(undefined)
	const [note, setNote] = useState("")
	const [state, setState] = useState<"review" | "sending" | "applied" | "error">("review")
	const [error, setError] = useState("")

	useEffect(() => {
		if (!token) {
			setPeek(null)
			return
		}
		pb.send<Peek>(`/api/beszel/maintenance/ack/peek?token=${encodeURIComponent(token)}`, { method: "GET" })
			.then((d) => setPeek(d?.valid ? d : null))
			.catch(() => setPeek(null))
	}, [token])

	async function confirm() {
		setState("sending")
		setError("")
		try {
			const r = await pb.send<{ ok?: boolean; reason?: string }>("/api/beszel/maintenance/ack/commit", {
				method: "POST",
				body: { token, note },
			})
			if (r?.ok) {
				setState("applied")
			} else {
				setState("error")
				setError(r?.reason ?? "unknown")
			}
		} catch (e) {
			setState("error")
			setError(String((e as Error)?.message || e))
		}
	}

	const days = peek?.days ?? Number(q.get("d") ?? 90)
	const title = peek?.title || "This alert"
	const fingerprint = peek?.fp || q.get("id") || ""

	return (
		<div className="mx-auto max-w-xl py-8">
			<div className="flex items-center gap-2 mb-6">
				<Logo className="h-6 w-6" />
				<span className="text-base font-semibold tracking-tight">
					Ohmz<span className="font-normal text-muted-foreground">Maintainer</span>
				</span>
			</div>

			<p className="text-xs uppercase tracking-wide text-muted-foreground mb-1">
				<Trans>Alert acknowledgement</Trans>
			</p>

			{peek === undefined ? (
				<p className="text-sm text-muted-foreground">
					<Trans>Checking this link…</Trans>
				</p>
			) : state === "applied" ? (
				<>
					<h1 className="text-2xl font-semibold mb-2">
						<Trans>Applied</Trans>
					</h1>
					<p className="text-sm text-muted-foreground">
						<Trans>
							This exact alert is silenced for {days} days. The runner is applying it now, about a minute.
						</Trans>
					</p>
				</>
			) : peek === null ? (
				<>
					<h1 className="text-2xl font-semibold mb-2">
						<Trans>This link is not valid</Trans>
					</h1>
					<p className="text-sm text-muted-foreground">
						<Trans>It may have expired or already been used. Open the dashboard to acknowledge from there.</Trans>
					</p>
				</>
			) : (
				<>
					<h1 className="text-2xl font-semibold mb-2">
						<Trans>Acknowledge this alert?</Trans>
					</h1>
					<div className="rounded-lg border border-border bg-card p-4 mb-4">
						<div className="flex items-center gap-2 mb-2">
							<span className={`block size-2 rounded-full ${sev === "crit" ? "bg-red-500" : "bg-yellow-500"}`} />
							<span className="text-sm font-medium">
								{sev === "crit" ? <Trans>Critical</Trans> : <Trans>Warning</Trans>}
							</span>
							<span className="ms-auto text-xs text-muted-foreground">
								<Trans>Silence for</Trans> {days} <Trans>days</Trans>
							</span>
						</div>
						<p className="text-sm">{title}</p>
						{fingerprint ? <p className="text-xs text-muted-foreground font-mono mt-2">ID {fingerprint}</p> : null}
					</div>

					<p className="text-sm text-muted-foreground mb-3">
						<Trans>
							This silences this exact alert. It stays listed as acknowledged, and a worse severity still alerts.
						</Trans>
					</p>

					<input
						value={note}
						onChange={(e) => setNote(e.target.value)}
						placeholder="Note (optional): why it is fine, for later"
						className="w-full h-9 rounded-md border border-border bg-background px-3 text-sm outline-none focus:ring-1 focus:ring-ring mb-4"
					/>

					<div className="flex items-center gap-2">
						<button
							type="button"
							onClick={confirm}
							disabled={state === "sending"}
							className="inline-flex h-10 items-center rounded-md bg-primary px-5 text-sm font-semibold text-primary-foreground hover:opacity-90 disabled:opacity-60"
						>
							{state === "sending" ? <Trans>Working…</Trans> : <Trans>Acknowledge for {days} days</Trans>}
						</button>
						<a href="/checks" className="text-sm text-muted-foreground hover:underline">
							<Trans>Open the dashboard</Trans>
						</a>
					</div>
					{state === "error" ? (
						<p className="text-sm text-red-500 mt-3">
							<Trans>Not applied</Trans> ({error}). <Trans>Try again, or acknowledge from the dashboard.</Trans>
						</p>
					) : null}
				</>
			)}
		</div>
	)
})
