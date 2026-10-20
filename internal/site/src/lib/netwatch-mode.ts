// Network Watch: which kind of block dialog a finding gets. Pure, so it can be checked without the page.
// "hard"   never blockable: no block target, a non-IP source, a hard-protected address or one inside a hard range.
// "signin" blockable only with a reason: a stolen-account flag, a signin-protected address, or a recent sign-in.
// "open"   blockable with no reason.
// It reads the whole document (talkers and protected rows), not the talkers list alone, which is capped.

export type BlockMode = "hard" | "signin" | "open"

type Addr = { v: 4 | 6; n: bigint }

const V4_RE = /^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/

function parseV4(s: string): bigint | null {
	if (!V4_RE.test(s)) return null
	let n = 0n
	for (const part of s.split(".")) {
		const b = Number(part)
		if (b > 255) return null
		n = (n << 8n) | BigInt(b)
	}
	return n
}

function parseV6(input: string): bigint | null {
	let s = input
	const zone = s.indexOf("%")
	if (zone >= 0) s = s.slice(0, zone)
	const lastColon = s.lastIndexOf(":")
	if (lastColon < 0) return null
	if (s.slice(lastColon + 1).includes(".")) {
		const v4 = parseV4(s.slice(lastColon + 1))
		if (v4 === null) return null
		s = `${s.slice(0, lastColon + 1)}${((v4 >> 16n) & 0xffffn).toString(16)}:${(v4 & 0xffffn).toString(16)}`
	}
	const halves = s.split("::")
	if (halves.length > 2) return null
	const head = halves[0] ? halves[0].split(":") : []
	let groups: string[]
	if (halves.length === 2) {
		const rest = halves[1] ? halves[1].split(":") : []
		const fill = 8 - head.length - rest.length
		if (fill < 1) return null
		groups = [...head, ...new Array<string>(fill).fill("0"), ...rest]
	} else {
		groups = head
	}
	if (groups.length !== 8) return null
	let n = 0n
	for (const g of groups) {
		if (!/^[0-9a-fA-F]{1,4}$/.test(g)) return null
		n = (n << 16n) | BigInt(Number.parseInt(g, 16))
	}
	return n
}

// An IPv4 or IPv6 address as a number. IPv4-mapped IPv6 (::ffff:a.b.c.d) is the IPv4 address.
export function parseAddr(text: string): Addr | null {
	const s = text.trim()
	const v4 = parseV4(s)
	if (v4 !== null) return { v: 4, n: v4 }
	if (!s.includes(":")) return null
	const v6 = parseV6(s)
	if (v6 === null) return null
	if (v6 >> 32n === 0xffffn) return { v: 4, n: v6 & 0xffffffffn }
	return { v: 6, n: v6 }
}

// Whether `ip` is inside the CIDR `cidr`. A malformed range or address contains nothing.
export function cidrContains(cidr: string, ip: string): boolean {
	const [netText, prefixText, extra] = cidr.trim().split("/")
	if (extra !== undefined || prefixText === undefined || !/^\d+$/.test(prefixText)) return false
	const net = parseAddr(netText)
	const addr = parseAddr(ip)
	if (!net || !addr) return false
	const total = net.v === 4 ? 32 : 128
	// A mapped-IPv6 range (::ffff:a.b.c.d/N) is parsed as IPv4, so its prefix is counted from the IPv6 bits.
	const prefix = Number(prefixText) - (cidr.includes("::ffff:") && net.v === 4 ? 96 : 0)
	if (prefix < 0 || prefix > total) return false
	if (addr.v !== net.v) return false
	const shift = BigInt(total - prefix)
	return addr.n >> shift === net.n >> shift
}

export function sameAddr(a: string, b: string): boolean {
	const x = parseAddr(a)
	const y = parseAddr(b)
	return !!x && !!y && x.v === y.v && x.n === y.n
}

export type FindingLike = { src?: string; block?: string | null; stolen_account?: string | null }
export type TalkerLike = { ip: string; protection?: string | null }
export type ProtectedLike = { signins?: { ip: string }[]; hard_ranges?: string[] }

export function blockMode(f: FindingLike, talkers: TalkerLike[], doc: ProtectedLike): BlockMode {
	const src = f.src ?? ""
	if (!f.block || !parseAddr(src)) return "hard"
	const protection = talkers.find((t) => sameAddr(t.ip, src))?.protection ?? null
	if (protection === "hard") return "hard"
	if ((doc.hard_ranges ?? []).some((r) => cidrContains(r, src))) return "hard"
	if (f.stolen_account || protection === "signin" || (doc.signins ?? []).some((s) => sameAddr(s.ip, src))) {
		return "signin"
	}
	return "open"
}
