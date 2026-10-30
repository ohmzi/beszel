import * as React from "react"

import { cn } from "@/lib/utils"

type TableProps = React.HTMLAttributes<HTMLTableElement> & {
	/** On phones, lay each row out as a stacked card instead of squeezing the columns (see .table-stack). */
	stack?: boolean
}

const Table = React.forwardRef<HTMLTableElement, TableProps>(({ className, stack, ...props }, ref) => {
	const own = React.useRef<HTMLTableElement | null>(null)
	const setRef = React.useCallback(
		(el: HTMLTableElement | null) => {
			own.current = el
			if (typeof ref === "function") ref(el)
			else if (ref) ref.current = el
		},
		[ref]
	)

	// Stacked rows lose the header, so each cell carries its column name for the ::before label.
	// Runs after every render because rows come and go with the data.
	React.useLayoutEffect(() => {
		const table = own.current
		if (!stack || !table) return
		const labels = Array.from(table.querySelectorAll("thead tr:last-child th"), (th) => th.textContent?.trim() ?? "")
		for (const row of table.querySelectorAll<HTMLTableRowElement>("tbody tr")) {
			let col = 0
			for (const cell of row.cells) {
				const label = cell.colSpan === 1 ? labels[col] : ""
				if (label) cell.dataset.label = label
				else delete cell.dataset.label
				col += cell.colSpan
			}
		}
	})

	return (
		<div className="relative w-full overflow-auto">
			<table
				ref={setRef}
				className={cn("w-full caption-bottom text-sm", stack && "table-stack", className)}
				{...props}
			/>
		</div>
	)
})
Table.displayName = "Table"

const TableHeader = React.forwardRef<HTMLTableSectionElement, React.HTMLAttributes<HTMLTableSectionElement>>(
	({ className, ...props }, ref) => (
		<thead
			ref={ref}
			className={cn("bg-table-header border-b border-border/50 [&_tr]:border-b", className)}
			{...props}
		/>
	)
)
TableHeader.displayName = "TableHeader"

const TableBody = React.forwardRef<HTMLTableSectionElement, React.HTMLAttributes<HTMLTableSectionElement>>(
	({ className, ...props }, ref) => (
		<tbody ref={ref} className={cn("[&_tr:last-child]:border-0", className)} {...props} />
	)
)
TableBody.displayName = "TableBody"

const TableFooter = React.forwardRef<HTMLTableSectionElement, React.HTMLAttributes<HTMLTableSectionElement>>(
	({ className, ...props }, ref) => (
		<tfoot ref={ref} className={cn("border-t bg-muted/50 font-medium last:[&>tr]:border-b-0", className)} {...props} />
	)
)
TableFooter.displayName = "TableFooter"

const TableRow = React.forwardRef<HTMLTableRowElement, React.HTMLAttributes<HTMLTableRowElement>>(
	({ className, ...props }, ref) => (
		<tr
			ref={ref}
			className={cn(
				"border-b border-border/60 hover:bg-muted/40 dark:hover:bg-muted/20 data-[state=selected]:bg-muted/40!",
				className
			)}
			{...props}
		/>
	)
)
TableRow.displayName = "TableRow"

const TableHead = React.forwardRef<HTMLTableCellElement, React.ThHTMLAttributes<HTMLTableCellElement>>(
	({ className, ...props }, ref) => (
		<th
			ref={ref}
			className={cn(
				"h-12 px-4 text-start align-middle font-medium text-muted-foreground [&:has([role=checkbox])]:pe-0",
				className
			)}
			{...props}
		/>
	)
)
TableHead.displayName = "TableHead"

const TableCell = React.forwardRef<HTMLTableCellElement, React.TdHTMLAttributes<HTMLTableCellElement>>(
	({ className, ...props }, ref) => (
		<td ref={ref} className={cn("p-4 align-middle [&:has([role=checkbox])]:pe-0", className)} {...props} />
	)
)
TableCell.displayName = "TableCell"

const TableCaption = React.forwardRef<HTMLTableCaptionElement, React.HTMLAttributes<HTMLTableCaptionElement>>(
	({ className, ...props }, ref) => (
		<caption ref={ref} className={cn("mt-4 text-sm text-muted-foreground", className)} {...props} />
	)
)
TableCaption.displayName = "TableCaption"

export { Table, TableHeader, TableBody, TableFooter, TableHead, TableRow, TableCell, TableCaption }
