// Ohmz fork: the monitoring-pipeline strip shown under the navbar on every page.
// It answers "is the thing that produces this data still running?" from the engine's self.json,
// manifest.json and overview.json — read-only, polled like the other maintenance files.
import { Trans } from "@lingui/react/macro"
import { useState } from "react"
import { dotFor, dur, useMaintenanceFile, when } from "@/lib/maintenance"

type SelfCheck = { id?: string; title?: string; state?: string; detail?: string; age_s?: number; limit_s?: number }
type Self = {
	level?: string
	headline?: string
	generated_at?: number
	valid_until?: number
	verdict?: { level?: string; reasons?: string[]; since?: number }
	checks?: SelfCheck[]
}
type Manifest = {
	schema?: number
	generated_at?: number
	runner_version?: string
	registry_hash?: string
	registry_synced_at?: number
	rules_count?: number
	registry_valid?: boolean
}
type Overview = { generated_at?: number; export_errors?: string[] }

export function PipelineStrip() {
	const self = useMaintenanceFile<Self>("self.json")
	const manifest = useMaintenanceFile<Manifest>("manifest.json")
	const overview = useMaintenanceFile<Overview>("overview.json")
	const [open, setOpen] = useState(false)

	if (!self && !manifest) return null

	const level = self?.level ?? "unknown"
	const reasons = self?.verdict?.reasons ?? []
	const parts = self?.checks ?? []
	const healthy = level === "ok" || level === "info"

	return (
		<div className="mb-4 rounded-md border border-border bg-card text-sm">
			<button
				type="button"
				onClick={() => setOpen((v) => !v)}
				className="w-full flex items-center gap-2 px-3 py-2 text-left"
			>
				<span className={`block size-2 rounded-full shrink-0 ${dotFor(level)}`} />
				<span className="truncate">
					{self?.headline ??
						(healthy ? (
							<Trans>Monitoring pipeline: reported on time</Trans>
						) : (
							<Trans>Monitoring pipeline: no answer yet</Trans>
						))}
					{reasons.length ? ` — ${reasons[0]}` : ""}
				</span>
				<span className="ms-auto text-xs text-muted-foreground shrink-0">{open ? "Hide details" : "Details"}</span>
			</button>

			{open ? (
				<div className="border-t border-border px-3 py-3 space-y-3">
					{reasons.length ? (
						<div>
							<div className="text-xs uppercase tracking-wide text-muted-foreground mb-1">
								<Trans>Verdict</Trans>
							</div>
							<ul className="text-sm list-disc ps-5 text-muted-foreground">
								{reasons.map((r, i) => (
									<li key={i}>{r}</li>
								))}
							</ul>
						</div>
					) : null}

					{parts.length ? (
						<div>
							<div className="text-xs uppercase tracking-wide text-muted-foreground mb-1">
								<Trans>Parts of the pipeline</Trans>
							</div>
							<div className="grid gap-x-4 sm:grid-cols-2">
								{parts.map((c) => (
									<div key={c.id} className="flex items-center gap-2 py-0.5">
										<span className={`block size-2 rounded-full shrink-0 ${dotFor(c.state)}`} />
										<span className="truncate">{c.title || c.id}</span>
										<span className="ms-auto text-xs text-muted-foreground truncate">
											{typeof c.age_s === "number" ? dur(c.age_s) : ""}
											{c.limit_s ? ` / ${dur(c.limit_s)}` : ""}
										</span>
									</div>
								))}
							</div>
						</div>
					) : null}

					<div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
						{overview?.generated_at ? (
							<span>
								<Trans>Status data</Trans> {when(overview.generated_at)}
							</span>
						) : null}
						{manifest?.generated_at ? (
							<span>
								<Trans>Last publish</Trans> {when(manifest.generated_at)}
							</span>
						) : null}
						{manifest?.runner_version ? <span>runner v{manifest.runner_version}</span> : null}
						{typeof manifest?.rules_count === "number" ? <span>{manifest.rules_count} rules</span> : null}
						{manifest ? (
							<span className={manifest.registry_valid ? "" : "text-red-500"}>
								<Trans>registry</Trans> {manifest.registry_valid ? "valid" : "does not validate"}
								{manifest.registry_synced_at ? ` · synced ${when(manifest.registry_synced_at)}` : ""}
							</span>
						) : null}
					</div>

					{overview?.export_errors?.length ? (
						<div className="text-xs text-red-500">{overview.export_errors.join("; ")}</div>
					) : null}
				</div>
			) : null}
		</div>
	)
}
