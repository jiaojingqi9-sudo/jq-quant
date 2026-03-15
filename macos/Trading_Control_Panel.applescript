set repoRoot to "/Users/jiao/All here/trade"
set commandPath to repoRoot & "/Launch_Trading_Control_Panel.command"
set launcherCommand to "open -a Terminal " & quoted form of commandPath

try
	do shell script launcherCommand
on error errText
	display alert "Trading Control Panel failed" message errText as critical
end try
