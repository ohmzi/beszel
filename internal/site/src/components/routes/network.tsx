// Network Watch (Ohmz fork, see docs/OHMZ-REDESIGN.md).
// The maintenance engine publishes network.json: flagged findings, the top talkers, services by peer count,
// the active blocks and the never-block lists. Block and unblock forward a signed request to the agent, which
// writes it into the engine's inbox; the runner applies it within a minute. Blocks are never automatic.
// The e-mail link opens /network?finding=<id>, which selects the Flags tab and opens the block dialog pre-filled.
import { Trans, useLingui } from "@lingui/react/macro"
import { useStore } from "@nanostores/react"
import { type ReactNode, memo, useEffect, useRef, useState } from "react"
import { FooterRepoLink } from "@/components/footer-repo-link"
import { $router } from "@/components/router"
import { Badge } from "@/components/ui/badge"
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { pb } from "@/lib/api"
import { blockMode } from "@/lib/netwatch-mode"
import { useMaintenanceFile, when } from "@/lib/maintenance"
import { $systems } from "@/lib/stores"

type Finding = {
	id: string
	rule?: string
	severity?: string
	src?: string
	target?: string
	why?: string
	first?: number
	last?: number
	count?: number
	block?: string | null
	zone?: string | null
	stolen_account?: string | null
}
type Talker = {
	ip: string
	country?: string
	asn?: number
	hosts?: string[]
	requests?: number
	threat?: string[] | null
	protection?: string | null
}
type Service = { name: string; in: number; out: number }
type Block = {
	id: string
	ip: string
	target: string
	zone: string
	until: number | null
	permanent: boolean
	reason: string
}
type Network = {
	available?: boolean
	generated?: number
	learning_until?: number | null
	sources?: Record<string, string>
	talkers?: Talker[]
	services?: Service[]
	findings?: Finding[]
	blocks?: Block[]
	protected?: { signins?: { ip: string; source: string; last_seen: number }[]; hard_ranges?: string[] }
}
type Note = { key: string; text: string; ok: boolean }

const SEV_DOT: Record<string, string> = { crit: "bg-red-500", warn: "bg-yellow-500" }

export default memo(() => {
	const { t } = useLingui()
	const systems = useStore($systems)
	useStore($router) // re-render when the route (and so the query string) changes
	const net = useMaintenanceFile<Network>("network.json")
	const finding = new URLSearchParams(window.location.search).get("finding") ?? ""
	const live = !!net && net.available !== false
	const talkers = net?.talkers ?? []
	const findings = net?.findings ?? []
	const blocks = net?.blocks ?? []
	const host = systems.find((s) => s.info?.mt)
	const stale = Object.entries(net?.sources ?? {})
		.filter(([, state]) => state !== "ok" && state !== "skipped")
		.map(([name]) => name)

	const [tab, setTab] = useState("flags")
	const [dialog, setDialog] = useState<Finding | null>(null)
	const [busy, setBusy] = useState("")
	const [note, setNote] = useState<Note | null>(null)
	const opened = useRef("")

	useEffect(() => {
		document.title = "OhmzMaintainer"
	}, [t])

	// The e-mail link selects the Flags tab. Keyed on the finding only, so a 30 s refresh does not snap the tab back.
	useEffect(() => {
		if (finding) setTab("flags")
	}, [finding])

	// The e-mail link opens the block dialog for that finding, once per finding.
	useEffect(() => {
		if (!finding) return
		const f = findings.find((x) => x.id === finding)
		if (f && opened.current !== finding) {
			opened.current = finding
			setDialog(f)
		}
	}, [finding, net])

	const hasRow = findings.some((x) => x.id === finding)
	useEffect(() => {
		if (!finding || tab !== "flags" || !hasRow) return
		document.getElementById(`finding-${finding}`)?.scrollIntoView({ block: "center" })
	}, [finding, tab, hasRow])

	// Sends a signed request to the agent; resolves to null on success or the server's error text.
	async function send(key: string, body: Record<string, unknown>): Promise<string | null> {
		if (!host) return t`No maintenance agent is connected, so the request cannot be sent.`
		setBusy(key)
		setNote(null)
		try {
			await pb.send("/api/beszel/maintenance/netblock", { method: "POST", query: { system: host.id }, body })
			return null
		} catch (err) {
			return String((err as Error)?.message || err)
		} finally {
			setBusy("")
		}
	}

	function ack(key: string, err: string | null) {
		setNote(err ? { key, text: err, ok: false } : { key, text: t`Requested — applied within a minute`, ok: true })
	}

	return (
		<>
			<div className="mb-4">
				<h1 className="text-2xl font-semibold mb-1">
					<Trans>Network</Trans>
				</h1>
				<p className="text-sm text-muted-foreground">
					<Trans>What the network watch saw, the top talkers and the blocks in force.</Trans>
				</p>
				{live && net?.learning_until && net.learning_until * 1000 > Date.now() ? (
					<p className="text-xs text-muted-foreground mt-1">
						<Trans>Learning period ends</Trans> {when(net.learning_until)}
					</p>
				) : null}
			</div>
			{!live ? (
				<div className="rounded-lg border border-border bg-card px-4 py-10 text-center text-muted-foreground text-sm">
					<Trans>The network watch has no data yet.</Trans>
				</div>
			) : (
				<>
					{stale.length > 0 ? (
						<div className="mb-4 rounded-lg border border-yellow-500/50 bg-yellow-500/10 px-4 py-3 text-sm">
							{t`Some sources are stale: ${stale.join(", ")} — data may be incomplete`}
						</div>
					) : null}
					<Tabs value={tab} onValueChange={setTab}>
						<TabsList>
							<TabsTrigger value="flags">
								<Trans>Flags</Trans>
							</TabsTrigger>
							<TabsTrigger value="talkers">
								<Trans>Talkers</Trans>
							</TabsTrigger>
							<TabsTrigger value="services">
								<Trans>Services</Trans>
							</TabsTrigger>
							<TabsTrigger value="blocks">
								<Trans>Blocks</Trans>
							</TabsTrigger>
							<TabsTrigger value="never">
								<Trans>Never-block</Trans>
							</TabsTrigger>
						</TabsList>

						<TabsContent value="flags">
							{findings.length === 0 ? (
								<Empty>
									{stale.length > 0 ? (
										<Trans>No open findings in the data available</Trans>
									) : (
										<Trans>No open findings.</Trans>
									)}
								</Empty>
							) : (
								<Table>
									<TableHeader className="sticky top-0 z-10 bg-table-header">
										<TableRow>
											<TableHead className="w-28">
												<Trans>Severity</Trans>
											</TableHead>
											<TableHead className="w-44">
												<Trans>Rule</Trans>
											</TableHead>
											<TableHead>
												<Trans>Source → target</Trans>
											</TableHead>
											<TableHead>
												<Trans>Why</Trans>
											</TableHead>
											<TableHead className="w-44">
												<Trans>Seen</Trans>
											</TableHead>
											<TableHead className="w-36 text-right">
												<Trans>Action</Trans>
											</TableHead>
										</TableRow>
									</TableHeader>
									<TableBody>
										{findings.map((f) => (
											<TableRow
												key={f.id}
												id={`finding-${f.id}`}
												className={f.id === finding ? "bg-accent/40" : undefined}
											>
												<TableCell>
													<span className="flex items-center gap-1.5">
														<span
															className={`block size-2 rounded-full ${SEV_DOT[f.severity ?? ""] ?? "bg-foreground/40"}`}
														/>
														<span className="text-muted-foreground">
															{f.severity === "crit" ? t`critical` : t`warning`}
														</span>
													</span>
												</TableCell>
												<TableCell className="font-medium">{f.rule ?? ""}</TableCell>
												<TableCell className="tabular-nums">
													{f.src ?? ""} <span className="text-muted-foreground">→</span> {f.target ?? ""}
												</TableCell>
												<TableCell>
													<span className="block text-sm">{f.why ?? ""}</span>
													{f.stolen_account ? (
														<span className="block text-xs text-red-500 mt-1">
															<Trans>Possible stolen account:</Trans> {f.stolen_account}
														</span>
													) : null}
													{note && note.key === f.id ? (
														<span className={`block text-xs mt-1 ${note.ok ? "text-green-500" : "text-red-500"}`}>
															{note.text}
														</span>
													) : null}
												</TableCell>
												<TableCell className="text-xs text-muted-foreground whitespace-nowrap">
													<span className="block">
														<Trans>first</Trans> {when(f.first)}
													</span>
													<span className="block">
														<Trans>last</Trans> {when(f.last)}
													</span>
												</TableCell>
												<TableCell className="text-right">
													{f.block ? (
														<button
															type="button"
															className="inline-flex h-8 items-center rounded-md border border-border bg-card px-3 text-xs font-medium hover:bg-accent disabled:opacity-60"
															onClick={() => setDialog(f)}
														>
															<Trans>Block</Trans>
														</button>
													) : null}
												</TableCell>
											</TableRow>
										))}
									</TableBody>
								</Table>
							)}
						</TabsContent>

						<TabsContent value="talkers">
							{talkers.length === 0 ? (
								<Empty>
									<Trans>No talkers in the last day.</Trans>
								</Empty>
							) : (
								<Table>
									<TableHeader className="sticky top-0 z-10 bg-table-header">
										<TableRow>
											<TableHead>
												<Trans>Address</Trans>
											</TableHead>
											<TableHead className="w-20">
												<Trans>Country</Trans>
											</TableHead>
											<TableHead className="w-24">
												<Trans>ASN</Trans>
											</TableHead>
											<TableHead>
												<Trans>Hosts</Trans>
											</TableHead>
											<TableHead className="w-28 text-right">
												<Trans>Requests</Trans>
											</TableHead>
											<TableHead className="w-32">
												<Trans>Threat</Trans>
											</TableHead>
											<TableHead className="w-32">
												<Trans>Protection</Trans>
											</TableHead>
										</TableRow>
									</TableHeader>
									<TableBody>
										{talkers.map((r) => (
											<TableRow key={r.ip}>
												<TableCell className="font-mono text-xs">{r.ip}</TableCell>
												<TableCell className="text-muted-foreground">{r.country || "-"}</TableCell>
												<TableCell className="tabular-nums text-muted-foreground">{r.asn || "-"}</TableCell>
												<TableCell className="text-xs text-muted-foreground">{(r.hosts ?? []).join(", ")}</TableCell>
												<TableCell className="text-right tabular-nums">{r.requests ?? 0}</TableCell>
												<TableCell>
													{r.threat === null || r.threat === undefined ? (
														<span className="text-xs text-muted-foreground">
															<Trans>unknown</Trans>
														</span>
													) : r.threat.length === 0 ? (
														<span className="text-xs text-muted-foreground">
															<Trans>none</Trans>
														</span>
													) : (
														<Badge variant="danger">{r.threat.join(", ")}</Badge>
													)}
												</TableCell>
												<TableCell>
													{r.protection === "hard" ? (
														<Badge variant="secondary">
															<Trans>never block</Trans>
														</Badge>
													) : r.protection === "signin" ? (
														<Badge variant="warning">
															<Trans>signed in</Trans>
														</Badge>
													) : (
														<span className="text-xs text-muted-foreground">-</span>
													)}
												</TableCell>
											</TableRow>
										))}
									</TableBody>
								</Table>
							)}
						</TabsContent>

						<TabsContent value="services">
							{(net?.services ?? []).length === 0 ? (
								<Empty>
									<Trans>No services talked to a peer in the last day.</Trans>
								</Empty>
							) : (
								<Table>
									<TableHeader className="sticky top-0 z-10 bg-table-header">
										<TableRow>
											<TableHead>
												<Trans>Entity</Trans>
											</TableHead>
											<TableHead className="w-32 text-right">
												<Trans>Peers in</Trans>
											</TableHead>
											<TableHead className="w-32 text-right">
												<Trans>Peers out</Trans>
											</TableHead>
										</TableRow>
									</TableHeader>
									<TableBody>
										{(net?.services ?? []).map((s) => (
											<TableRow key={s.name}>
												<TableCell className="font-medium">{s.name}</TableCell>
												<TableCell className="text-right tabular-nums">{s.in}</TableCell>
												<TableCell className="text-right tabular-nums">{s.out}</TableCell>
											</TableRow>
										))}
									</TableBody>
								</Table>
							)}
						</TabsContent>

						<TabsContent value="blocks">
							{blocks.length === 0 ? (
								<Empty>
									<Trans>No active blocks.</Trans>
								</Empty>
							) : (
								<Table>
									<TableHeader className="sticky top-0 z-10 bg-table-header">
										<TableRow>
											<TableHead>
												<Trans>Address</Trans>
											</TableHead>
											<TableHead className="w-28">
												<Trans>Target</Trans>
											</TableHead>
											<TableHead className="w-36">
												<Trans>Zone</Trans>
											</TableHead>
											<TableHead className="w-32">
												<Trans>Until</Trans>
											</TableHead>
											<TableHead>
												<Trans>Reason</Trans>
											</TableHead>
											<TableHead className="w-32 text-right">
												<Trans>Action</Trans>
											</TableHead>
										</TableRow>
									</TableHeader>
									<TableBody>
										{blocks.map((b) => (
											<TableRow key={b.id}>
												<TableCell className="font-mono text-xs">{b.ip}</TableCell>
												<TableCell className="text-muted-foreground">{b.target}</TableCell>
												<TableCell className="text-muted-foreground">{b.zone || "-"}</TableCell>
												<TableCell className="tabular-nums text-muted-foreground whitespace-nowrap">
													{b.permanent ? t`permanent` : when(b.until)}
												</TableCell>
												<TableCell className="text-sm">
													{b.reason}
													{note && note.key === b.id ? (
														<span className={`block text-xs mt-1 ${note.ok ? "text-green-500" : "text-red-500"}`}>
															{note.text}
														</span>
													) : null}
												</TableCell>
												<TableCell className="text-right">
													<button
														type="button"
														className="inline-flex h-8 items-center rounded-md border border-border bg-card px-3 text-xs font-medium hover:bg-accent disabled:opacity-60"
														disabled={busy !== ""}
														onClick={async () => ack(b.id, await send(b.id, { kind: "net_unblock", block_id: b.id }))}
													>
														{busy === b.id ? t`Sending…` : t`Unblock`}
													</button>
												</TableCell>
											</TableRow>
										))}
									</TableBody>
								</Table>
							)}
						</TabsContent>

						<TabsContent value="never" className="space-y-6">
							<section>
								<h2 className="text-sm font-semibold mb-1">
									<Trans>Hard ranges</Trans>
								</h2>
								<p className="text-xs text-muted-foreground mb-2">
									<Trans>Never blocked, whatever the reason.</Trans>
								</p>
								{(net?.protected?.hard_ranges ?? []).length === 0 ? (
									<Empty>
										<Trans>No hard ranges.</Trans>
									</Empty>
								) : (
									<ul className="flex flex-wrap gap-2">
										{(net?.protected?.hard_ranges ?? []).map((r) => (
											<li key={r} className="rounded-md border border-border bg-card px-2 py-1 font-mono text-xs">
												{r}
											</li>
										))}
									</ul>
								)}
							</section>
							<section>
								<h2 className="text-sm font-semibold mb-1">
									<Trans>Signed in recently</Trans>
								</h2>
								<p className="text-xs text-muted-foreground mb-2">
									<Trans>Refused unless the block gives a reason, which is audited.</Trans>
								</p>
								{(net?.protected?.signins ?? []).length === 0 ? (
									<Empty>
										<Trans>No sign-ins in the last 30 days.</Trans>
									</Empty>
								) : (
									<Table>
										<TableHeader className="sticky top-0 z-10 bg-table-header">
											<TableRow>
												<TableHead>
													<Trans>Address</Trans>
												</TableHead>
												<TableHead className="w-40">
													<Trans>Source</Trans>
												</TableHead>
												<TableHead className="w-40">
													<Trans>Last seen</Trans>
												</TableHead>
											</TableRow>
										</TableHeader>
										<TableBody>
											{(net?.protected?.signins ?? []).map((s) => (
												<TableRow key={`${s.ip}-${s.source}`}>
													<TableCell className="font-mono text-xs">{s.ip}</TableCell>
													<TableCell className="text-muted-foreground">{s.source}</TableCell>
													<TableCell className="tabular-nums text-muted-foreground whitespace-nowrap">
														{when(s.last_seen)}
													</TableCell>
												</TableRow>
											))}
										</TableBody>
									</Table>
								)}
							</section>
						</TabsContent>
					</Tabs>
				</>
			)}
			{dialog ? (
				<BlockDialog
					finding={dialog}
					mode={blockMode(dialog, talkers, net?.protected ?? {})}
					onClose={() => setDialog(null)}
					onSubmit={async (body) => {
						const err = await send(dialog.id, body)
						if (!err) {
							ack(dialog.id, null)
							setDialog(null)
						}
						return err
					}}
				/>
			) : null}
			<FooterRepoLink />
		</>
	)
})

function Empty({ children }: { children: ReactNode }) {
	return (
		<div className="rounded-lg border border-border bg-card px-4 py-8 text-center text-muted-foreground text-sm">
			{children}
		</div>
	)
}

function BlockDialog({
	finding,
	mode,
	onClose,
	onSubmit,
}: {
	finding: Finding
	mode: "hard" | "signin" | "open"
	onClose: () => void
	onSubmit: (body: Record<string, unknown>) => Promise<string | null>
}) {
	const { t } = useLingui()
	const [days, setDays] = useState("30")
	const [permanent, setPermanent] = useState(false)
	const [reason, setReason] = useState("")
	const [sending, setSending] = useState(false)
	const [error, setError] = useState("")

	const daysOk = permanent || (/^\d+$/.test(days) && Number(days) >= 1 && Number(days) <= 365)
	const reasonOk = mode !== "signin" || reason.trim() !== ""
	const canSubmit = mode !== "hard" && daysOk && reasonOk && !sending
	const zone = finding.zone ? ` · ${finding.zone}` : ""

	async function submit() {
		setSending(true)
		setError("")
		const err = await onSubmit({
			kind: "net_block",
			finding: finding.id,
			ip: finding.src ?? "",
			target: finding.block ?? "",
			zone: finding.zone ?? "",
			days: permanent ? 365 : Number(days),
			permanent,
			override_reason: mode === "signin" ? reason.trim() : "",
		})
		setSending(false)
		if (err) setError(err)
	}

	return (
		<Dialog open onOpenChange={(open) => !open && onClose()}>
			<DialogContent>
				<DialogHeader>
					<DialogTitle>
						<Trans>Block {finding.src ?? ""}</Trans>
					</DialogTitle>
					<DialogDescription>
						{finding.rule ?? ""} · {finding.why ?? ""}
					</DialogDescription>
				</DialogHeader>
				<p className="text-sm">
					<Trans>Target</Trans>: {finding.block ?? "-"}
					{zone}
				</p>
				{mode === "hard" ? (
					<p className="text-sm text-red-500">
						{finding.block ? (
							<Trans>Never blocked: this address is in a protected range, so there is nothing to confirm.</Trans>
						) : (
							<Trans>This finding has no block target, so it cannot be blocked.</Trans>
						)}
					</p>
				) : null}
				{mode === "signin" ? (
					<p className="text-sm text-yellow-600 dark:text-yellow-400">
						<Trans>
							This address signed in successfully recently, or its account was reported stolen. A reason is required,
							and it is audited.
						</Trans>
					</p>
				) : null}
				{mode !== "hard" ? (
					<div className="grid gap-4">
						<div className="flex items-end gap-4">
							<div className="grid gap-1.5">
								<Label htmlFor="net-days">
									<Trans>Days</Trans>
								</Label>
								<Input
									id="net-days"
									type="number"
									min={1}
									max={365}
									value={days}
									disabled={permanent}
									onChange={(e) => setDays(e.target.value)}
									className="w-28"
								/>
							</div>
							<div className="flex items-center gap-2 pb-2">
								<Switch id="net-permanent" checked={permanent} onCheckedChange={setPermanent} />
								<Label htmlFor="net-permanent">
									<Trans>Permanent</Trans>
								</Label>
							</div>
						</div>
						{mode === "signin" ? (
							<div className="grid gap-1.5">
								<Label htmlFor="net-reason">
									<Trans>Reason (required)</Trans>
								</Label>
								<Input
									id="net-reason"
									value={reason}
									placeholder={t`Why this address should be blocked`}
									onChange={(e) => setReason(e.target.value)}
								/>
							</div>
						) : null}
					</div>
				) : null}
				{error ? <p className="text-sm text-red-500">{error}</p> : null}
				<DialogFooter>
					<button
						type="button"
						className="inline-flex h-9 items-center rounded-md border border-border bg-card px-4 text-sm font-medium hover:bg-accent"
						onClick={onClose}
					>
						<Trans>Cancel</Trans>
					</button>
					{mode !== "hard" ? (
						<button
							type="button"
							className="inline-flex h-9 items-center rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground disabled:opacity-50"
							disabled={!canSubmit}
							onClick={submit}
						>
							{sending ? t`Sending…` : t`Block`}
						</button>
					) : null}
				</DialogFooter>
			</DialogContent>
		</Dialog>
	)
}
