// OhmzMaintainer mark (Ohmz fork): an amber tile with the Ω glyph over a rising usage line.
export function Logo({ className }: { className?: string }) {
	return (
		<svg
			xmlns="http://www.w3.org/2000/svg"
			viewBox="0 0 40 40"
			className={className}
			role="img"
			aria-label="OhmzMaintainer"
		>
			<rect width="40" height="40" rx="11" fill="var(--color-primary)" />
			<g fill="none" stroke="var(--color-primary-foreground)" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
				{/* omega: a circle open at the bottom, two feet and a bar */}
				<path d="M13.5 22V18.5a6.5 6.5 0 0 0 13 0V22" />
				<path d="M13.5 22h13" />
				{/* usage line */}
				<path d="M8.5 32.5l4.3-3.4 2.9 2.6 4.4-6.2 3.4 3.9 5.2-6.4" opacity="0.92" />
			</g>
		</svg>
	)
}
