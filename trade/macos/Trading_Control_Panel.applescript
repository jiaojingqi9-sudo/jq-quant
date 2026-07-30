-- Assumed layout: this repo sits at ~/All here/trade. If you clone it
-- somewhere else, change the line below to your own repository path.
set repoRoot to (POSIX path of (path to home folder)) & "All here/trade"
set commandPath to repoRoot & "/stock/launchers/Launch_Trading_Control_Panel.command"
set launcherCommand to "open -a Terminal " & quoted form of commandPath

try
	do shell script launcherCommand
on error errText
	display alert "Trading Control Panel failed" message errText as critical
end try
