// ops-guard: GET /guard (request "state", trigger load) -> data.state.*; footprint 3x3 board tracks (212 px each).
// Lines starting with // are build-time comments and are stripped. Rules: widgets/CONVENTIONS.md.
<Stack gap={7} p={2} style={{opacity:data.state.stale?0.55:1}}>
<Group justify="space-between" align="center" wrap="nowrap" gap={8}>
<Group gap={7} wrap="nowrap" align="center">
<ColorSwatch color={"var(--mantine-color-"+(data.state.stale?"gray":data.state.skc||"gray")+"-"+(data.state.skc==="red"?6:5)+")"} size={8} withShadow={false} />
<Text fz={11} fw={800} tt="uppercase" lts="0.18em">Guard</Text>
</Group>
<Group gap={5} wrap="nowrap">
{data.state.stale&&<Badge size="xs" radius="sm" variant="filled" color="gray">{data.state.ago&&data.state.ago!=="never"?"stale "+data.state.ago:"no data"}</Badge>}
<Badge size="xs" radius="sm" variant="light" color={data.state.stale?"gray":data.state.skc||"gray"} c={"light-dark(color-mix(in srgb,var(--mantine-color-"+(data.state.stale?"gray":data.state.skc||"gray")+"-9),#000 30%),var(--mantine-color-"+(data.state.stale?"gray":data.state.skc||"gray")+"-light-color))"}>{data.state.head||"no data"}</Badge>
</Group>
</Group>
{status.state?.ok===false&&<Text fz={11} c="light-dark(color-mix(in srgb,var(--mantine-color-red-9),#000 30%),var(--mantine-color-red-5))" lineClamp={2}>{"ops service: "+(status.state.error||"no response")}</Text>}
{data.state.error&&<Text fz={11} c="light-dark(color-mix(in srgb,var(--mantine-color-orange-9),#000 30%),var(--mantine-color-orange-5))" lineClamp={2}>{data.state.error}</Text>}
<SimpleGrid cols={3} spacing={6}>
{(data.state.mem||[]).map((x,i)=>
<Paper key={i} radius="md" px={8} py={6} bg="light-dark(rgba(0,0,0,0.035),rgba(255,255,255,0.045))" style={{border:"1px solid light-dark(rgba(0,0,0,0.09),rgba(255,255,255,0.08))"}}>
<Text fz={10} fw={700} c="dimmed" tt="uppercase" lts="0.1em" truncate="end">{x.l}</Text>
<Text fz={14} fw={700} lh={1.15} c={data.state.mc==="gray"?"dimmed":"light-dark(color-mix(in srgb,var(--mantine-color-"+data.state.mc+"-9),#000 30%),var(--mantine-color-"+data.state.mc+"-5))"} truncate="end">{x.v}</Text>
</Paper>
)}
</SimpleGrid>
{data.state.mn&&<Text fz={11} c="dimmed" lh={1.25} lineClamp={2}>{data.state.mn}</Text>}
<Divider />
<Text fz={10} fw={700} c="dimmed" tt="uppercase" lts="0.12em">Stuck candidates</Text>
{(data.state.sk||[]).length===0&&<Text fz={11} c="dimmed" lh={1.25} lineClamp={2}>{data.state.sks||"no data"}</Text>}
{(data.state.sk||[]).map((x,i)=>
<Group key={i} gap={7} wrap="nowrap" align="center">
<ColorSwatch color={"var(--mantine-color-"+x.c+"-5)"} size={8} withShadow={false} />
<Text fz={12} fw={700} truncate="end" style={{flex:1,minWidth:0}}>{x.n}</Text>
<Text fz={11} c="dimmed" truncate="end" maw={110}>{x.r}</Text>
{x.k&&<Badge size="xs" radius="sm" variant="outline" color="gray">{x.k}</Badge>}
<Badge size="xs" radius="sm" variant="light" color={x.c} c={"light-dark(color-mix(in srgb,var(--mantine-color-"+x.c+"-9),#000 30%),var(--mantine-color-"+x.c+"-light-color))"}>{x.h}</Badge>
</Group>
)}
{(data.state.top||[]).length>0&&<Group gap={5} align="center">
<Text fz={10} fw={700} c="dimmed" tt="uppercase" lts="0.1em">{"Top of "+(data.state.cn||"?")+" ("+(data.state.ta||"?")+")"}</Text>
{(data.state.top||[]).map((x,i)=><Badge key={i} size="xs" radius="sm" variant="light" tt="none" color="gray">{x.n+" "+x.h}</Badge>)}
</Group>}
<Divider />
{(data.state.ln||[]).map((x,i)=>
<Group key={i} gap={7} wrap="nowrap" align="flex-start">
<ColorSwatch color={"var(--mantine-color-"+x.c+"-"+(x.c==="red"?6:5)+")"} size={8} withShadow={false} mt={4} />
<Stack gap={0} style={{flex:1,minWidth:0}}>
<Text fz={11} fw={700} lh={1.2}>{x.l}</Text>
<Text fz={11} c="dimmed" lh={1.25} lineClamp={2}>{x.s}</Text>
</Stack>
</Group>
)}
</Stack>
