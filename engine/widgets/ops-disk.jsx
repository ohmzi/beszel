// ops-disk: GET /disk (request "state", trigger load) -> data.state.*; footprint 3x3 board tracks (212 px each).
// Lines starting with // are build-time comments and are stripped. Rules: widgets/CONVENTIONS.md.
<Stack gap={7} p={2} style={{opacity:data.state.stale?0.55:1}}>
<Group justify="space-between" align="center" wrap="nowrap" gap={8}>
<Group gap={7} wrap="nowrap" align="center">
<ColorSwatch color={"var(--mantine-color-"+(data.state.stale?"gray":data.state.dc||"gray")+"-"+(data.state.dc==="red"?6:5)+")"} size={8} withShadow={false} />
<Text fz={11} fw={800} tt="uppercase" lts="0.18em">Disk</Text>
</Group>
<Group gap={5} wrap="nowrap">
{data.state.stale&&<Badge size="xs" radius="sm" variant="filled" color="gray">{data.state.ago&&data.state.ago!=="never"?"stale "+data.state.ago:"no data"}</Badge>}
<Badge size="xs" radius="sm" variant="light" color={data.state.stale?"gray":data.state.dc||"gray"} c={"light-dark(color-mix(in srgb,var(--mantine-color-"+(data.state.stale?"gray":data.state.dc||"gray")+"-9),#000 30%),var(--mantine-color-"+(data.state.stale?"gray":data.state.dc||"gray")+"-light-color))"}>{data.state.head||"no data"}</Badge>
</Group>
</Group>
{status.state?.ok===false&&<Text fz={11} c="light-dark(color-mix(in srgb,var(--mantine-color-red-9),#000 30%),var(--mantine-color-red-5))" lineClamp={2}>{"ops service: "+(status.state.error||"no response")}</Text>}
{data.state.error&&<Text fz={11} c="light-dark(color-mix(in srgb,var(--mantine-color-orange-9),#000 30%),var(--mantine-color-orange-5))" lineClamp={2}>{data.state.error}</Text>}
{(data.state.mounts||[]).map((x,i)=>
<Stack key={i} gap={3}>
<Group justify="space-between" align="baseline" wrap="nowrap" gap={6}>
<Text fz={12} fw={700} truncate="end" style={{flex:1,minWidth:0}}>{x.m}</Text>
<Text fz={11} c="dimmed" truncate="end">{x.f+" free"}</Text>
<Text fz={11} fw={700} c={x.c==="gray"?"dimmed":"light-dark(color-mix(in srgb,var(--mantine-color-"+x.c+"-9),#000 30%),var(--mantine-color-"+x.c+"-5))"} ta="right" miw={96}>{x.p+"%"+(x.d?" · full "+x.d:"")}</Text>
</Group>
<Progress value={x.p} size={6} radius="xl" color={x.i?"gray.5":x.c+".5"} />
</Stack>
)}
{data.state.more>0&&<Text fz={10} c="dimmed" ta="right">{"+"+data.state.more+" more mounts"}</Text>}
{(data.state.dk||[]).length>0&&<Divider />}
<SimpleGrid cols={4} spacing={6}>
{(data.state.dk||[]).map((x,i)=>
<Stack key={i} gap={0}>
<Text fz={10} fw={700} c="dimmed" tt="uppercase" lts="0.1em" truncate="end">{x.l}</Text>
<Text fz={12} fw={700} c={data.state.dkc==="gray"?"dimmed":"light-dark(color-mix(in srgb,var(--mantine-color-"+data.state.dkc+"-9),#000 30%),var(--mantine-color-"+data.state.dkc+"-5))"} truncate="end">{x.v}</Text>
</Stack>
)}
</SimpleGrid>
{(data.state.bk||[]).length>0&&<Group gap={5} align="center">
<Text fz={10} fw={700} c="dimmed" tt="uppercase" lts="0.1em" w={54} miw={54}>Backups</Text>
{(data.state.bk||[]).map((x,i)=><Badge key={i} size="xs" radius="sm" variant="light" tt="none" color={x.c} c={"light-dark(color-mix(in srgb,var(--mantine-color-"+x.c+"-9),#000 30%),var(--mantine-color-"+x.c+"-light-color))"}>{x.n+" "+x.a}</Badge>)}
</Group>}
{(data.state.sm||[]).length>0&&<Group gap={5} align="center">
<Text fz={10} fw={700} c="dimmed" tt="uppercase" lts="0.1em" w={54} miw={54}>SMART</Text>
{(data.state.sm||[]).map((x,i)=><Badge key={i} size="xs" radius="sm" variant="light" tt="none" color={x.c} c={"light-dark(color-mix(in srgb,var(--mantine-color-"+x.c+"-9),#000 30%),var(--mantine-color-"+x.c+"-light-color))"}>{x.n+" "+x.t+(x.x?" "+x.x:"")}</Badge>)}
</Group>}
{(data.state.gr||[]).length>0&&<Group gap={5} align="center">
<Text fz={10} fw={700} c="dimmed" tt="uppercase" lts="0.1em" w={54} miw={54}>Growth</Text>
{(data.state.gr||[]).map((x,i)=><Badge key={i} size="xs" radius="sm" variant="light" tt="none" color={x.c} c={"light-dark(color-mix(in srgb,var(--mantine-color-"+x.c+"-9),#000 30%),var(--mantine-color-"+x.c+"-light-color))"}>{x.p+" "+x.r}</Badge>)}
</Group>}
{data.state.plex&&data.state.plexc!=="teal"&&<Text fz={11} c={data.state.plexc==="gray"?"dimmed":"light-dark(color-mix(in srgb,var(--mantine-color-"+data.state.plexc+"-9),#000 30%),var(--mantine-color-"+data.state.plexc+"-5))"} lh={1.25} lineClamp={2}>{"Plex: "+data.state.plex}</Text>}
</Stack>
