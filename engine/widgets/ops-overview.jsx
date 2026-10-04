// ops-overview: GET /overview (request "state", trigger load) -> data.state.*; footprint 3x2 board tracks (212 px each).
// Lines starting with // are build-time comments and are stripped. Rules: widgets/CONVENTIONS.md.
<Stack gap={7} p={2} style={{opacity:data.state.stale?0.55:1}}>
<Group justify="space-between" align="center" wrap="nowrap" gap={8}>
<Group gap={7} wrap="nowrap" align="center">
<ColorSwatch color={"var(--mantine-color-"+(data.state.c||"gray")+"-"+(data.state.c==="red"?6:5)+")"} size={8} withShadow={false} />
<Text fz={11} fw={800} tt="uppercase" lts="0.18em">Ops</Text>
</Group>
<Group gap={5} wrap="nowrap">
{data.state.paused&&<Badge size="xs" radius="sm" variant="filled" color="orange.5" c="dark.9">paused</Badge>}
{data.state.stale&&<Badge size="xs" radius="sm" variant="filled" color="gray">{data.state.ago&&data.state.ago!=="never"?"stale "+data.state.ago:"no data"}</Badge>}
<Badge size="xs" radius="sm" variant="light" color={data.state.c||"gray"} c={"light-dark(color-mix(in srgb,var(--mantine-color-"+(data.state.c||"gray")+"-9),#000 30%),var(--mantine-color-"+(data.state.c||"gray")+"-light-color))"}>{data.state.head||"no data"}</Badge>
</Group>
</Group>
{status.state?.ok===false&&<Text fz={11} c="light-dark(color-mix(in srgb,var(--mantine-color-red-9),#000 30%),var(--mantine-color-red-5))" lineClamp={2}>{"ops service: "+(status.state.error||"no response")}</Text>}
{data.state.error&&<Text fz={11} c="light-dark(color-mix(in srgb,var(--mantine-color-orange-9),#000 30%),var(--mantine-color-orange-5))" lineClamp={2}>{data.state.error}</Text>}
<SimpleGrid cols={3} spacing={6}>
{(data.state.tiles||[]).map((x,i)=>
<Paper key={i} radius="md" p={8} bg="light-dark(rgba(0,0,0,0.035),rgba(255,255,255,0.045))" style={{border:"1px solid light-dark(rgba(0,0,0,0.09),rgba(255,255,255,0.08))"}}>
<Text fz={10} fw={700} c="dimmed" tt="uppercase" lts="0.12em" truncate="end">{x.l}</Text>
<Text fz={18} fw={700} lh={1.15} c={x.c==="gray"?"dimmed":"light-dark(color-mix(in srgb,var(--mantine-color-"+x.c+"-9),#000 30%),var(--mantine-color-"+x.c+"-5))"} truncate="end">{x.v}</Text>
<Text fz={10} c="dimmed" truncate="end">{x.s}</Text>
</Paper>
)}
</SimpleGrid>
{data.state.level&&data.state.level!=="none"&&!data.state.error&&data.state.sub&&(data.state.issues||[]).length===0&&<Text fz={12} c="dimmed" ta="center" py={4}>all checks clear</Text>}
{(data.state.issues||[]).map((x,i)=>
<Group key={i} gap={7} wrap="nowrap" align="flex-start">
<ColorSwatch color={"var(--mantine-color-"+x.c+"-"+(x.c==="red"?6:5)+")"} size={8} withShadow={false} mt={4} />
<Stack gap={0} style={{flex:1,minWidth:0}}>
<Text fz={12} fw={700} lh={1.2} truncate="end">{x.n}</Text>
<Text fz={11} c="dimmed" lh={1.25} lineClamp={2}>{x.s}</Text>
</Stack>
</Group>
)}
<Divider />
<Group justify="space-between" align="center" wrap="nowrap" gap={6}>
<Group gap={5} wrap="nowrap">
{(data.state.tiers||[]).map((x,i)=><Badge key={i} size="xs" radius="sm" variant="light" color={x.c} c={"light-dark(color-mix(in srgb,var(--mantine-color-"+x.c+"-9),#000 30%),var(--mantine-color-"+x.c+"-light-color))"}>{x.n+" "+x.a}</Badge>)}
</Group>
<Text fz={10} c="dimmed" truncate="end">{data.state.ap_all>0?"cleanup apply "+data.state.ap_on+"/"+data.state.ap_all:""}</Text>
</Group>
<Text fz={10} c="dimmed" lh={1.2} truncate="end">{data.state.sub}</Text>
</Stack>
