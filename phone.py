"""Phone helpers. Phone Link dialling is disabled — Sonali talks on the laptop.

  # IPhoneLink   commented out. Used to type numbers on Phone Link's keypad.
  AndroidPhone if `adb devices` shows a phone. Unused while --dial is off.

Standard library only. call.py uses normalise_number and laptop audio only.
"""
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


def find_adb():
    candidates = [
        shutil.which("adb"),
        Path(__file__).parent / "platform-tools" / "adb.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk" / "platform-tools" / "adb.exe",
    ]
    return next((str(p) for p in candidates if p and Path(p).exists()), None)


def normalise_number(raw, country_code="+91"):
    """'+91 90000 00002', '09000000002' or '9000000002' -> '+919000000002'."""
    number = re.sub(r"[^\d+]", "", raw or "")
    if number.startswith("+"):
        return number
    number = number.lstrip("0")
    if len(number) == 10:
        return country_code + number
    if len(number) == 12 and number.startswith(country_code.lstrip("+")):
        return "+" + number
    return number


def android_ready():
    adb = find_adb()
    if not adb:
        return False
    try:
        done = subprocess.run([adb, "get-state"], capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return (done.stdout or "").strip() == "device"


def connect_phone():
    """Phone Link is disabled. Laptop speaker/mic only."""
    # if android_ready():
    #     return AndroidPhone()
    # return IPhoneLink()
    sys.exit("Phone Link is disabled. Run: python call.py --limit 1 --verbose")


def _shell_open(uri):
    """Ask Windows to open a URI. Returns True if ShellExecute accepted it."""
    import ctypes
    rc = ctypes.windll.shell32.ShellExecuteW(None, "open", uri, None, None, 1)
    return rc > 32


def _ps(script, timeout=20):
    done = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-Command", script],
        capture_output=True, text=True, timeout=timeout)
    return (done.stdout or "") + (done.stderr or "")


def _ps_file(script, timeout=45):
    path = Path(os.environ.get("TEMP", ".")) / "sonali_phone_link_dial.ps1"
    path.write_text(script, encoding="utf-8")
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
             "-File", str(path)],
            capture_output=True, text=True, timeout=timeout)
        return (done.stdout or "") + (done.stderr or "")
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def _is_call_active():
    """True while Phone Link / Bluetooth HFP has an active cellular call."""
    out = _ps(
        "[Windows.ApplicationModel.Calls.PhoneCallManager,"
        "Windows.ApplicationModel.Calls,ContentType=WindowsRuntime] | Out-Null; "
        "[Windows.ApplicationModel.Calls.PhoneCallManager]::IsCallActive")
    return "True" in out.splitlines()[-1] if out.strip() else False


def _safe_tel_number(number):
    cleaned = re.sub(r"[^\d+]", "", number or "")
    if not re.fullmatch(r"\+?\d{8,15}", cleaned):
        sys.exit(f"Refusing to dial an unrecognised number: {number!r}")
    return cleaned


# Types the sheet number on Phone Link's keypad (Button0–Button9, ButtonCall)
# and presses Call. tel: is not used: Windows has no handler and shows
# "Pick an app". The WinUI window is also missing from UIA's root children,
# so we find it with EnumWindows (title Phone Link).
_PHONE_LINK_KEYPAD_PS = r'''
$ErrorActionPreference = 'Continue'
$Number = '__NUMBER__'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class SonaliWin {
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lp);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder sb, int max);
  [DllImport("user32.dll")] public static extern int GetClassName(IntPtr hWnd, StringBuilder sb, int max);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extra);
  [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
}
"@

function Get-PhoneLinkHwnd {
  $script:found = [IntPtr]::Zero
  $script:fallback = [IntPtr]::Zero
  $cb = [SonaliWin+EnumProc]{
    param($h, $lp)
    if (-not [SonaliWin]::IsWindowVisible($h)) { return $true }
    $t = New-Object System.Text.StringBuilder 256
    $c = New-Object System.Text.StringBuilder 256
    [SonaliWin]::GetWindowText($h, $t, 256) | Out-Null
    [SonaliWin]::GetClassName($h, $c, 256) | Out-Null
    if ($t.ToString() -eq 'Phone Link') { $script:found = $h; return $false }
    if ($c.ToString() -eq 'WinUIDesktopWin32WindowClass' -and $script:fallback -eq [IntPtr]::Zero) {
      $script:fallback = $h
    }
    return $true
  }
  [SonaliWin]::EnumWindows($cb, [IntPtr]::Zero) | Out-Null
  if ($script:found -ne [IntPtr]::Zero) { return $script:found }
  return $script:fallback
}

function Find-Id($win, $autoId) {
  $cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::AutomationIdProperty, $autoId)
  return $win.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
}

function Click-El($el) {
  # Click this control's own rectangle. Do NOT use GetClickablePoint:
  # on the Phone Link keypad that point is the group centre, which is the 8 key.
  if (-not $el) { return $false }
  $id = [string]$el.Current.AutomationId
  if ($id -eq 'Keypad') { return $false }
  $r = $el.Current.BoundingRectangle
  if ($r.Width -lt 8 -or $r.Height -lt 8) { return $false }
  # Digit keys are small. A huge rect is the keypad group — never click it.
  if ($id -match '^Button[0-9]$' -and ($r.Width -gt 160 -or $r.Height -gt 160)) { return $false }
  $x = [int]($r.X + $r.Width / 2)
  $y = [int]($r.Y + $r.Height / 2)
  if ($x -le 0 -or $y -le 0) { return $false }
  [SonaliWin]::SetCursorPos($x, $y) | Out-Null
  Start-Sleep -Milliseconds 50
  [SonaliWin]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 40
  [SonaliWin]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
  return $true
}

function Enum-Uia($result) {
  $list = New-Object System.Collections.Generic.List[object]
  if ($null -eq $result) { return $list }
  if ($result -is [System.Windows.Automation.AutomationElement]) { $list.Add($result); return $list }
  foreach ($el in $result) { if ($null -ne $el) { $list.Add($el) } }
  return $list
}

$deadline = (Get-Date).AddSeconds(15)
$hwnd = [IntPtr]::Zero
$win = $null
while ((Get-Date) -lt $deadline) {
  $hwnd = Get-PhoneLinkHwnd
  if ($hwnd -ne [IntPtr]::Zero) {
    try {
      $win = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
      if ($win) { break }
    } catch {}
  }
  Start-Sleep -Milliseconds 400
}
if (-not $win) { Write-Output 'NO_WINDOW'; exit 1 }

[SonaliWin]::ShowWindow($hwnd, 9) | Out-Null
[SonaliWin]::SetForegroundWindow($hwnd) | Out-Null
Start-Sleep -Milliseconds 400

# Keypad is already on the Calls page. Clicking the tab's centre (or the
# Keypad group) lands on the 8 key and prepends 8 to every number.
if (-not (Find-Id $win 'Button0')) {
  $calls = Find-Id $win 'CallingNodeAutomationId'
  if ($calls) {
    $r = $calls.Current.BoundingRectangle
    $x = [int]($r.X + [Math]::Min(36, [Math]::Max(8, $r.Width / 5)))
    $y = [int]($r.Y + $r.Height / 2)
    if ($x -gt 0 -and $y -gt 0) {
      [SonaliWin]::SetCursorPos($x, $y) | Out-Null
      Start-Sleep -Milliseconds 50
      [SonaliWin]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
      Start-Sleep -Milliseconds 40
      [SonaliWin]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
      Write-Output 'CLICKED_CALLS_TAB'
      Start-Sleep -Milliseconds 800
    }
  }
}

function Get-AccumDigits($win) {
  $acc = Find-Id $win 'AccumulatorTextBlock'
  $sub = Find-Id $win 'AccumulatorSubtextBlock'
  $shown = ''
  if ($acc) { $shown += [string]$acc.Current.Name }
  if ($sub) { $shown += [string]$sub.Current.Name }
  return ($shown -replace '\D', '')
}

function Find-Delete($win) {
  foreach ($id in @('ButtonDelete','ButtonBackspace','DeleteButton','BackspaceButton','DialerBackspace')) {
    $el = Find-Id $win $id
    if ($el) { return $el }
  }
  $cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Button)
  foreach ($el in (Enum-Uia ($win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)))) {
    $n = [string]$el.Current.Name
    $id = [string]$el.Current.AutomationId
    if ($n -match 'Backspace|Delete' -or $id -match 'Delete|Backspace') { return $el }
  }
  return $null
}

function Clear-Dialer($win) {
  # Never click Keypad or Accumulator: both resolve to the 8 key.
  $del = Find-Delete $win
  $n = 0
  while ((Get-AccumDigits $win) -ne '' -and $n -lt 24) {
    if ($del) { Click-El $del | Out-Null } else {
      [SonaliWin]::keybd_event(0x08, 0, 0, [UIntPtr]::Zero)
      [SonaliWin]::keybd_event(0x08, 0, 2, [UIntPtr]::Zero)
    }
    Start-Sleep -Milliseconds 40
    $n++
  }
}

function Type-Digits($win, $digits) {
  $i = 0
  foreach ($ch in $digits.ToCharArray()) {
    $btn = Find-Id $win ('Button' + $ch)
    if (-not (Click-El $btn)) { return $false }
    Start-Sleep -Milliseconds 180
    $i++
    $got = Get-AccumDigits $win
    $want = $digits.Substring(0, $i)
    if ($got -ne $want) {
      Write-Output ('DIGIT_MISMATCH want=' + $want + ' got=' + $got)
      return $false
    }
  }
  return $true
}

$digits = ($Number -replace '[^\d]', '')
$got = ''
$typed = $false
$attempt = 0
while ($attempt -lt 3 -and -not $typed) {
  $attempt++
  Clear-Dialer $win
  $left = Get-AccumDigits $win
  if ($left -ne '') { Write-Output ('STILL_HAS ' + $left) }
  if (-not (Type-Digits $win $digits)) {
    Write-Output 'NO_DIGIT'
    continue
  }
  Start-Sleep -Milliseconds 250
  $got = Get-AccumDigits $win
  if ($got -eq $digits) { $typed = $true }
  else { Write-Output ('ACCUM_RETRY ' + $got) }
}
Write-Output ('TYPED ' + $digits)
Write-Output ('ACCUM ' + $got)
if ($got -ne $digits) {
  Write-Output 'NUMBER_NOT_ON_DIALER'
  exit 1
}

Start-Sleep -Milliseconds 300
$call = Find-Id $win 'ButtonCall'
if (-not (Click-El $call)) {
  Write-Output 'NO_CALL_BUTTON'
  exit 1
}
Write-Output 'CLICKED_CALL'
Start-Sleep -Milliseconds 800
$confirm = Find-Id $win 'ButtonCall'
if ($confirm) { Click-El $confirm | Out-Null }
exit 0
'''


def _national_digits(number):
    """Phone Link's keypad already assumes +91. Type the 10-digit mobile only."""
    digits = re.sub(r"\D", "", number or "")
    if digits.startswith("91") and len(digits) >= 12:
        digits = digits[2:]
    return digits


def _press_phone_link_keypad(number):
    digits = _national_digits(number)
    print(f"   [phone] keypad digits {digits}")
    script = _PHONE_LINK_KEYPAD_PS.replace("__NUMBER__", digits)
    return _ps_file(script, timeout=45)


_PHONE_LINK_HANGUP_PS = r'''
$ErrorActionPreference = 'Continue'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class SonaliWin {
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lp);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder sb, int max);
  [DllImport("user32.dll")] public static extern int GetClassName(IntPtr hWnd, StringBuilder sb, int max);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extra);
}
"@

function Get-PhoneLinkHwnd {
  $script:found = [IntPtr]::Zero
  $cb = [SonaliWin+EnumProc]{
    param($h, $lp)
    if (-not [SonaliWin]::IsWindowVisible($h)) { return $true }
    $t = New-Object System.Text.StringBuilder 256
    [SonaliWin]::GetWindowText($h, $t, 256) | Out-Null
    if ($t.ToString() -eq 'Phone Link') { $script:found = $h; return $false }
    return $true
  }
  [SonaliWin]::EnumWindows($cb, [IntPtr]::Zero) | Out-Null
  return $script:found
}

function Enum-Uia($result) {
  $list = New-Object System.Collections.Generic.List[object]
  if ($null -eq $result) { return $list }
  if ($result -is [System.Windows.Automation.AutomationElement]) { $list.Add($result); return $list }
  foreach ($el in $result) { if ($null -ne $el) { $list.Add($el) } }
  return $list
}

function Click-El($el) {
  if (-not $el) { return $false }
  $x = 0; $y = 0
  try {
    $pt = $el.GetClickablePoint()
    $x = [int]$pt.X; $y = [int]$pt.Y
  } catch {
    $r = $el.Current.BoundingRectangle
    if ($r.Width -lt 2 -or $r.Height -lt 2) { return $false }
    $x = [int]($r.X + $r.Width / 2)
    $y = [int]($r.Y + $r.Height / 2)
  }
  if ($x -le 0 -or $y -le 0) { return $false }
  [SonaliWin]::SetCursorPos($x, $y) | Out-Null
  Start-Sleep -Milliseconds 50
  [SonaliWin]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 40
  [SonaliWin]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
  return $true
}

$script:hwnds = New-Object System.Collections.Generic.List[IntPtr]
$cbAll = [SonaliWin+EnumProc]{
  param($h, $lp)
  if ([SonaliWin]::IsWindowVisible($h)) { $script:hwnds.Add($h) }
  return $true
}
[SonaliWin]::EnumWindows($cbAll, [IntPtr]::Zero) | Out-Null
$hwnd = Get-PhoneLinkHwnd
if ($hwnd -ne [IntPtr]::Zero) { $script:hwnds.Insert(0, $hwnd) }

$pat = 'End call|Hang up|^End$|EndCall|HangUp|EndCallButton'
$cond = New-Object System.Windows.Automation.PropertyCondition(
  [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
  [System.Windows.Automation.ControlType]::Button)
$clicked = $false
$win = $null
foreach ($h in $script:hwnds) {
  try { $cand = [System.Windows.Automation.AutomationElement]::FromHandle($h) } catch { continue }
  if (-not $cand) { continue }
  foreach ($el in (Enum-Uia ($cand.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)))) {
    $n = [string]$el.Current.Name
    $id = [string]$el.Current.AutomationId
    if ($n -match $pat -or $id -match $pat) {
      $win = $cand
      $hwnd = $h
      [SonaliWin]::ShowWindow($hwnd, 9) | Out-Null
      [SonaliWin]::SetForegroundWindow($hwnd) | Out-Null
      Start-Sleep -Milliseconds 200
      if (Click-El $el) {
        Write-Output ('CLICKED_END ' + $n + ' ' + $id)
        $clicked = $true
        break
      }
    }
  }
  if ($clicked) { break }
}
if (-not $clicked) {
  Write-Output 'NO_END_BUTTON'
  exit 1
}
Start-Sleep -Milliseconds 500
foreach ($el in (Enum-Uia ($win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)))) {
  $n = [string]$el.Current.Name
  if ($n -match '^(End call|Hang up|End|Yes)$') { Click-El $el | Out-Null; break }
}
Write-Output 'HUNG_UP'
exit 0
'''


def _hang_up_phone_link():
    return _ps_file(_PHONE_LINK_HANGUP_PS, timeout=20)


_PHONE_LINK_WAIT_ANSWERED_PS = r'''
$ErrorActionPreference = 'Continue'
$TimeoutSec = [int]'__TIMEOUT__'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class SonaliWin {
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lp);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder sb, int max);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
}
"@
function Get-PhoneLinkHwnd {
  $script:found = [IntPtr]::Zero
  $cb = [SonaliWin+EnumProc]{
    param($h, $lp)
    if (-not [SonaliWin]::IsWindowVisible($h)) { return $true }
    $t = New-Object System.Text.StringBuilder 256
    [SonaliWin]::GetWindowText($h, $t, 256) | Out-Null
    if ($t.ToString() -eq 'Phone Link') { $script:found = $h; return $false }
    return $true
  }
  [SonaliWin]::EnumWindows($cb, [IntPtr]::Zero) | Out-Null
  return $script:found
}
function Enum-Uia($result) {
  $list = New-Object System.Collections.Generic.List[object]
  if ($null -eq $result) { return $list }
  if ($result -is [System.Windows.Automation.AutomationElement]) { $list.Add($result); return $list }
  foreach ($el in $result) { if ($null -ne $el) { $list.Add($el) } }
  return $list
}
function Get-UiText($win) {
  $cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Text)
  $parts = @()
  foreach ($el in (Enum-Uia ($win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)))) {
    $n = [string]$el.Current.Name
    if ($n) { $parts += $n }
  }
  return ($parts -join ' | ')
}
$deadline = (Get-Date).AddSeconds($TimeoutSec)
$btnCond = New-Object System.Windows.Automation.PropertyCondition(
  [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'EndCallButton')
$script:hwnds = New-Object System.Collections.Generic.List[IntPtr]
$cbAll = [SonaliWin+EnumProc]{
  param($h, $lp)
  if ([SonaliWin]::IsWindowVisible($h)) { $script:hwnds.Add($h) }
  return $true
}
while ((Get-Date) -lt $deadline) {
  $script:hwnds.Clear()
  [SonaliWin]::EnumWindows($cbAll, [IntPtr]::Zero) | Out-Null
  $ph = Get-PhoneLinkHwnd
  if ($ph -ne [IntPtr]::Zero) { $script:hwnds.Insert(0, $ph) }
  $answered = $false
  $ui = ''
  foreach ($h in $script:hwnds) {
    try { $win = [System.Windows.Automation.AutomationElement]::FromHandle($h) } catch { continue }
    if (-not $win) { continue }
    $text = Get-UiText $win
    if ($text -match 'Calling|Ringing|Dialing|is calling') { continue }
    $endBtn = $win.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $btnCond)
    if ($endBtn -and $text -notmatch 'Search your contacts or dial a number') {
      $answered = $true
      $ui = $text
      break
    }
  }
  if ($answered) {
    Write-Output 'ANSWERED'
    if ($ui) { Write-Output ('UI ' + $ui.Substring(0, [Math]::Min(180, $ui.Length))) }
    exit 0
  }
  Start-Sleep -Milliseconds 600
}
Write-Output 'NOT_ANSWERED'
exit 1
'''


# Phone Link iPhone dialling — disabled. Sonali uses the laptop speaker/mic.
class IPhoneLink:
    """Disabled. Do not open Phone Link or place a cellular call."""

    talk_when_connected = True

    def __init__(self):
        # print("Phone: iPhone via Phone Link (Bluetooth Hands-Free)")
        # print("   Charging cable = power only. The call uses Bluetooth.")
        # _shell_open("ms-phone:calling")
        # time.sleep(1.2)
        raise RuntimeError("Phone Link is disabled. Use the laptop speaker and mic.")

    def dial(self, number, display_name="Customer"):
        # Phone Link keypad automation commented out.
        # number = _safe_tel_number(number)
        # _shell_open("ms-phone:calling")
        # pressed = _press_phone_link_keypad(number)
        raise RuntimeError("Phone Link is disabled")

    def in_call(self):
        return False

    def wait_until_dialling(self, timeout=45):
        # Phone Link IsCallActive poll — disabled.
        return False

    def wait_until_answered(self, timeout=40):
        # Phone Link EndCallButton wait — disabled.
        return False

    def hang_up(self):
        # print("   [phone] ending the Phone Link call")
        # _hang_up_phone_link()
        return


class AndroidPhone:
    talk_when_connected = False

    def __init__(self):
        self.adb = find_adb()
        if not self.adb:
            sys.exit("adb not found. Download Android platform-tools (free) from\n"
                     "https://developer.android.com/tools/releases/platform-tools\n"
                     "and unzip it into this folder (platform-tools\\adb.exe) or add it to PATH.")
        state = self._adb("get-state").strip()
        if state != "device":
            sys.exit("No phone found over adb. Connect it by USB, turn on USB debugging,\n"
                     "and accept the 'Allow USB debugging' prompt on the phone.")
        model = self._adb("shell", "getprop", "ro.product.model").strip()
        print(f"Phone: {model} (via adb)")

    def _adb(self, *args, check=False):
        try:
            done = subprocess.run([self.adb, *args], capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            return ""
        text = (done.stdout or "") + (done.stderr or "")
        if check and done.returncode != 0:
            return text
        return done.stdout if not check else text

    def dial(self, number, display_name="Customer"):
        uri = "tel:" + number.replace("+", "%2B")
        print(f"   [phone] adb CALL {number}")
        out = self._adb("shell", "am", "start", "-a", "android.intent.action.CALL",
                        "-d", uri, check=True)
        if out.strip():
            print(f"   [phone] {out.strip()}")
        if "Error" in out or "Exception" in out:
            print("   [phone] CALL blocked, opening dialer with the same number")
            self._adb("shell", "am", "start", "-a", "android.intent.action.DIAL", "-d", uri)

    def in_call(self):
        states = re.findall(r"mCallState=(\d)", self._adb("shell", "dumpsys", "telephony.registry"))
        return any(s != "0" for s in states)

    def wait_until_dialling(self, timeout=8):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.in_call():
                return True
            time.sleep(0.5)
        return False

    def hang_up(self):
        self._adb("shell", "input", "keyevent", "KEYCODE_ENDCALL")
