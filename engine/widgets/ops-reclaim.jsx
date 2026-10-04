// ops-reclaim: GET /reclaim (request "state", trigger load) -> data.state.*; footprint 3x3 board tracks (212 px each).
// Lines starting with // are build-time comments and are stripped. Rules: widgets/CONVENTIONS.md.
<Stack gap={7} p={2} style={{opacity:data.state.stale?0.55:1}}>
<Group justify="space-between" align="center" wrap="nowrap" gap={8}>
<Group gap={7} wrap="nowrap" align="center">
<ColorSwatch color={"var(--mantine-color-"+(data.state.stale||!data.state.head?"gray":"teal")+"-5)"} size={8} withShadow={false} />
<Text fz={11} fw={800} tt="uppercase" lts="0.18em">Reclaimed</Text>
</Group>
<Group gap={5} wrap="nowrap">
{data.state.stale&&<Badge size="xs" radius="sm" variant="filled" color="gray">{data.state.ago&&data.state.ago!=="never"?"stale "+data.state.ago:"no data"}</Badge>}
<Badge size="xs" radius="sm" variant="light" color={data.state.stale||!data.state.head?"gray":"teal"} c={"light-dark(color-mix(in srgb,var(--mantine-color-"+(data.state.stale||!data.state.head?"gray":"teal")+"-9),#000 30%),var(--mantine-color-"+(data.state.stale||!data.state.head?"gray":"teal")+"-light-color))"}>{data.state.head||"no data"}</Badge>
</Group>
</Group>
{status.state?.ok===false&&<Text fz={11} c="light-dark(color-mix(in srgb,var(--mantine-color-red-9),#000 30%),var(--mantine-color-red-5))" lineClamp={2}>{"ops service: "+(status.state.error||"no response")}</Text>}
{data.state.error&&<Text fz={11} c="light-dark(color-mix(in srgb,var(--mantine-color-orange-9),#000 30%),var(--mantine-color-orange-5))" lineClamp={2}>{data.state.error}</Text>}
<SimpleGrid cols={4} spacing={6}>
{[{l:"24 h",v:data.state.d1},{l:"7 d",v:data.state.d7},{l:"30 d",v:data.state.d30},{l:"90 d",v:data.state.d90}].map((x,i)=>
<Paper key={i} radius="md" px={8} py={6} bg="light-dark(rgba(0,0,0,0.035),rgba(255,255,255,0.045))" style={{border:"1px solid light-dark(rgba(0,0,0,0.09),rgba(255,255,255,0.08))"}}>
<Text fz={10} fw={700} c="dimmed" tt="uppercase" lts="0.1em">{x.l}</Text>
<Text fz={14} fw={700} lh={1.15} truncate="end">{x.v||"--"}</Text>
</Paper>
)}
</SimpleGrid>
{(data.state.by||[]).map((x,i)=>
<Stack key={i} gap={2}>
<Group justify="space-between" wrap="nowrap" gap={6}>
<Text fz={11} fw={700} truncate="end" style={{flex:1,minWidth:0}}>{x.n}</Text>
<Text fz={11} c="dimmed">{x.h+" in 30 d"}</Text>
</Group>
<Progress value={x.p} size={5} radius="xl" color="teal.5" />
</Stack>
)}
{(data.state.ev||[]).length>0&&<Text fz={10} fw={700} c="dimmed" tt="uppercase" lts="0.12em">Recent</Text>}
{(data.state.ev||[]).map((x,i)=>
<Group key={i} justify="space-between" wrap="nowrap" gap={6}>
<Text fz={11} truncate="end" style={{flex:1,minWidth:0}}>{x.n}</Text>
<Text fz={11} fw={700} c="light-dark(color-mix(in srgb,var(--mantine-color-teal-9),#000 30%),var(--mantine-color-teal-5))">{x.h}</Text>
<Text fz={10} c="dimmed" w={58} miw={58} ta="right">{x.w}</Text>
</Group>
)}
{(data.state.pr||[]).length>0&&<Divider />}
{(data.state.pr||[]).length>0&&<Group justify="space-between" wrap="nowrap">
<Text fz={10} fw={700} c="dimmed" tt="uppercase" lts="0.12em">Waiting to be cleaned</Text>
<Badge size="xs" radius="sm" variant="light" color="yellow" c="light-dark(color-mix(in srgb,var(--mantine-color-yellow-9),#000 30%),var(--mantine-color-yellow-light-color))">{data.state.pend}</Badge>
</Group>}
{(data.state.pr||[]).map((x,i)=>
<Group key={i} justify="space-between" wrap="nowrap" gap={6}>
<Text fz={11} truncate="end" style={{flex:1,minWidth:0}}>{x.n}</Text>
<Badge size="xs" radius="sm" variant="light" color={x.c}>{x.m}</Badge>
<Text fz={11} fw={700} w={64} miw={64} ta="right">{x.h}</Text>
</Group>
)}
{(data.state.plans||[]).length>0&&<Divider />}
{(data.state.plans||[]).map((x,i)=>
<Stack key={i} gap={3}>
<Group justify="space-between" wrap="nowrap" gap={6}>
<Text fz={11} fw={700} truncate="end">{x.n+" - needs approval"}</Text>
<Badge size="xs" radius="sm" variant="filled" color="yellow">{x.h+", "+x.k+" items"}</Badge>
</Group>
<Paper radius="sm" px={6} py={3} bg="light-dark(rgba(0,0,0,0.05),rgba(255,255,255,0.06))"><Text fz={10} ff="monospace" style={{wordBreak:"break-all"}}>{x.cmd}</Text></Paper>
</Stack>
)}
{(data.state.cand||[]).map((x,i)=>
<Group key={i} justify="space-between" wrap="nowrap" gap={6}>
<Text fz={11} truncate="end" style={{flex:1,minWidth:0}}>{x.n}</Text>
{x.m&&<Badge size="xs" radius="sm" variant="outline" color="yellow" c="light-dark(color-mix(in srgb,var(--mantine-color-yellow-9),#000 30%),var(--mantine-color-yellow-light-color))">check first</Badge>}
<Text fz={11} fw={700} c="dimmed" w={64} miw={64} ta="right">{x.h}</Text>
</Group>
)}
</Stack>
