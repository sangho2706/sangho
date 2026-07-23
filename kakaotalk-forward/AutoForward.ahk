#Requires AutoHotkey v2.0
#SingleInstance Force
; ============================================================================
;  카카오톡 PC 자동 전달 (AutoForward)
; ----------------------------------------------------------------------------
;  받은 메시지(텍스트 / 이미지 / 파일)를 카카오톡 "전달" 기능으로
;  지정한 한 사람에게 그대로 넘겨줍니다.  (오픈채팅 제외)
;
;  ※ "복사→붙여넣기"가 아니라 카카오톡 내장 "전달" 을 자동으로 눌러주는 방식이라
;    이미지·파일도 원본 그대로 전달됩니다.
;
;  ※ 카카오톡 UI는 버전/해상도/테마에 따라 위치가 다릅니다.
;    아래 CONFIG 값을 본인 PC에 맞게 한 번 보정(캘리브레이션)해야 합니다.
;    자세한 방법은 같은 폴더의 README.md 를 참고하세요.
; ============================================================================

; ==============================  CONFIG  ====================================
global CFG := {

    ; --- 전달 대상 (필수) ------------------------------------------------
    ; 전달받을 사람의 카카오톡 이름(또는 검색으로 특정되는 문구).
    ; 동명이인이 있으면 검색 결과 첫 번째가 선택되니 주의하세요.
    TargetName: "ㄲㅅㅎ",

    ; --- 동작 모드 -------------------------------------------------------
    ; "hotkey" : 내가 단축키를 누를 때만 현재 채팅의 최신 메시지를 전달 (안정적, 먼저 이걸로 보정)
    ; "auto"   : 새 메시지 알림 팝업을 감지해 자동 전달 (실험적, 보정 끝난 뒤 사용)
    Mode: "hotkey",

    ; --- 단축키 ----------------------------------------------------------
    ; hotkey 모드에서 사용할 전달 단축키 (기본: Ctrl+Alt+F)
    ForwardHotkey: "^!f",
    ; auto 모드 On/Off 토글 (기본: Ctrl+Alt+A)
    ToggleHotkey: "^!a",
    ; 긴급 중지 (기본: Ctrl+Alt+Q) — 실행 정지
    QuitHotkey: "^!q",

    ; --- 오픈채팅 제외 ---------------------------------------------------
    ; 채팅방/창 제목이 아래 정규식 중 하나에 맞으면 전달하지 않습니다.
    ; 오픈채팅 방 이름을 여기에 추가하거나, 더 확실히 하려면
    ; 카카오톡 설정에서 오픈채팅 알림을 꺼두세요(README 참고).
    ExcludeTitlePatterns: ["오픈채팅", "OpenChat"],

    ; --- 좌표/타이밍 보정값 (본인 PC에 맞게 조정) -----------------------
    ; 활성 채팅창에서 "최신 메시지 말풍선"을 우클릭할 위치.
    ; 채팅창 클라이언트 영역 좌하단 기준 오프셋(px).
    MsgOffsetX: 60,     ; 왼쪽 가장자리에서 오른쪽으로
    MsgOffsetY: 90,     ; 아래쪽(입력창 위) 가장자리에서 위로

    ; 우클릭 컨텍스트 메뉴에서 "전달" 까지 내려가는 방향키 횟수.
    ; (메뉴에서 Down 을 이 횟수만큼 누른 뒤 Enter)
    ForwardMenuDownCount: 1,

    ; 전달 대상 검색 후, 첫 번째 검색 결과 체크박스를 클릭할 위치.
    ; 전달 대화상자의 좌상단 기준 오프셋(px).
    PickFirstResultX: 40,
    PickFirstResultY: 150,

    ; 전달 대화상자의 "확인/전송" 버튼 위치 (대화상자 우하단 기준 오프셋, 음수).
    ConfirmBtnX: -70,
    ConfirmBtnY: -35,

    ; 각 단계 사이 대기시간(ms). 반응이 느리면 늘리세요.
    StepDelay: 350,
    DialogWait: 900,

    ; auto 모드 폴링 간격(ms)
    PollInterval: 700
}
; ============================  /CONFIG  =====================================


; ------------------------------  상태  --------------------------------------
global AutoOn := false
global Busy := false
global SeenPopups := Map()

; ------------------------------  트레이  ------------------------------------
A_IconTip := "카카오톡 자동전달 (대기)"
TrayMenuInit()

; ------------------------------  단축키  ------------------------------------
Hotkey(CFG.ForwardHotkey, (*) => SafeForwardCurrent())
Hotkey(CFG.ToggleHotkey,  (*) => ToggleAuto())
Hotkey(CFG.QuitHotkey,    (*) => ExitApp())

if (CFG.Mode = "auto")
    ToggleAuto()

Notify("준비 완료. 모드=" CFG.Mode "  전달대상=" CFG.TargetName
     . "`n" HotkeyLabel(CFG.ForwardHotkey) ": 지금 전달  /  "
     . HotkeyLabel(CFG.ToggleHotkey) ": 자동 On·Off  /  "
     . HotkeyLabel(CFG.QuitHotkey) ": 종료")
return


; ===========================  핵심 로직  ====================================

; 현재 활성 카카오톡 채팅창의 최신 메시지를 전달 (hotkey 모드)
SafeForwardCurrent(*) {
    global Busy
    if (Busy)
        return
    Busy := true
    try {
        hwnd := WinExist("A")
        if (!IsKakaoChat(hwnd)) {
            Notify("전달 실패: 카카오톡 채팅창이 활성 상태가 아닙니다.")
            return
        }
        title := WinGetTitle(hwnd)
        if (IsExcluded(title)) {
            Notify("건너뜀(오픈채팅/제외 대상): " title)
            return
        }
        ForwardLatestInWindow(hwnd)
        UpdateTip()
    } catch as e {
        Notify("오류: " e.Message)
    } finally {
        Busy := false
    }
}

; 주어진 채팅창에서 최신 메시지를 우클릭 → 전달 → 대상 선택 → 확인
ForwardLatestInWindow(hwnd) {
    WinActivate(hwnd)
    WinWaitActive("ahk_id " hwnd, , 2)

    ; 채팅창 클라이언트 영역 좌표 계산
    WinGetClientPos(&cx, &cy, &cw, &ch, hwnd)
    rcX := cx + CFG.MsgOffsetX
    rcY := cy + ch - CFG.MsgOffsetY

    ; 1) 최신 메시지 말풍선 우클릭 → 컨텍스트 메뉴
    MouseMove(rcX, rcY, 0)
    Sleep(120)
    Click(rcX, rcY, "Right")
    Sleep(CFG.StepDelay)

    ; 2) 메뉴에서 "전달" 선택 (Down N회 + Enter)
    Loop CFG.ForwardMenuDownCount
        Send("{Down}")
    Sleep(120)
    Send("{Enter}")

    ; 3) 전달 대화상자 대기
    Sleep(CFG.DialogWait)
    dlg := WaitForwardDialog(hwnd)
    if (!dlg)
        throw Error("전달 대화상자를 찾지 못했습니다 (DialogWait 늘려보세요).")

    ; 4) 대상 검색 (검색창이 기본 포커스라고 가정)
    WinActivate(dlg)
    Sleep(150)
    SendText(CFG.TargetName)
    Sleep(CFG.DialogWait)

    ; 5) 첫 번째 검색 결과 체크
    WinGetPos(&dx, &dy, &dw, &dh, dlg)
    Click(dx + CFG.PickFirstResultX, dy + CFG.PickFirstResultY)
    Sleep(CFG.StepDelay)

    ; 6) 확인/전송 버튼 클릭
    Click(dx + dw + CFG.ConfirmBtnX, dy + dh + CFG.ConfirmBtnY)
    Sleep(CFG.StepDelay)

    Notify("전달 완료 → " CFG.TargetName)
}


; ===========================  auto 모드  ====================================

ToggleAuto(*) {
    global AutoOn
    AutoOn := !AutoOn
    if (AutoOn) {
        SetTimer(PollNewMessage, CFG.PollInterval)
        Notify("자동 전달 ON")
    } else {
        SetTimer(PollNewMessage, 0)
        Notify("자동 전달 OFF")
    }
    UpdateTip()
}

; 카카오톡 새 메시지 알림 팝업을 감지 → 열어서 최신 메시지 전달
; (카카오톡 설정에서 '미리보기 알림 팝업'이 켜져 있어야 합니다)
PollNewMessage() {
    global Busy, SeenPopups
    if (Busy || !AutoOn)
        return

    ; 알림 팝업 후보 창들을 검사
    for hwnd in WinGetList("ahk_exe KakaoTalk.exe") {
        if (!IsToastPopup(hwnd))
            continue
        title := WinGetTitle(hwnd)
        if (SeenPopups.Has(hwnd))
            continue
        SeenPopups[hwnd] := A_TickCount

        if (IsExcluded(title))
            continue

        Busy := true
        try {
            ; 팝업 클릭 → 해당 채팅 열림
            WinActivate(hwnd)
            Sleep(150)
            ControlClick(, hwnd)
            Sleep(CFG.DialogWait)

            chat := WinExist("A")
            if (IsKakaoChat(chat) && !IsExcluded(WinGetTitle(chat)))
                ForwardLatestInWindow(chat)
        } catch as e {
            Notify("자동 전달 오류: " e.Message)
        } finally {
            Busy := false
        }
    }

    ; 오래된 기록 정리
    for h, t in SeenPopups.Clone()
        if (A_TickCount - t > 60000)
            SeenPopups.Delete(h)
}


; ===========================  판별 유틸  ====================================

IsKakaoChat(hwnd) {
    if (!hwnd)
        return false
    try exe := WinGetProcessName(hwnd)
    catch
        return false
    return (exe = "KakaoTalk.exe")
}

; 알림 토스트 팝업인지 대략 판별 (작은 크기 + 화면 우하단 근처)
IsToastPopup(hwnd) {
    try {
        cls := WinGetClass(hwnd)
        WinGetPos(&x, &y, &w, &h, hwnd)
    } catch
        return false
    if (w = 0 || h = 0)
        return false
    ; 토스트는 대체로 폭이 좁고(≈300~420) 높이가 낮음(≤200)
    return (w >= 200 && w <= 460 && h <= 220)
}

IsExcluded(title) {
    for pat in CFG.ExcludeTitlePatterns
        if (RegExMatch(title, pat))
            return true
    return false
}


; ===========================  대화상자 탐색  ================================

; 전달 대화상자로 추정되는 창을 잠시 기다렸다 반환
WaitForwardDialog(parent) {
    start := A_TickCount
    while (A_TickCount - start < 3000) {
        for hwnd in WinGetList("ahk_exe KakaoTalk.exe") {
            if (hwnd = parent)
                continue
            try WinGetPos(&x, &y, &w, &h, hwnd)
            catch
                continue
            ; 전달 대화상자는 중간 크기의 별도 창
            if (w >= 300 && w <= 900 && h >= 300 && h <= 900 && WinGetTitle(hwnd) != "")
                return hwnd
        }
        Sleep(120)
    }
    return 0
}


; ===========================  트레이/알림  ==================================

TrayMenuInit() {
    tray := A_TrayMenu
    tray.Delete()
    tray.Add("지금 전달 (" HotkeyLabel(CFG.ForwardHotkey) ")", (*) => SafeForwardCurrent())
    tray.Add("자동 On/Off (" HotkeyLabel(CFG.ToggleHotkey) ")", (*) => ToggleAuto())
    tray.Add()
    tray.Add("종료", (*) => ExitApp())
}

UpdateTip() {
    A_IconTip := "카카오톡 자동전달 (" (AutoOn ? "자동 ON" : "대기") ")"
}

Notify(msg) {
    TrayTip("카카오톡 자동전달", msg, 1)
}

HotkeyLabel(hk) {
    s := StrReplace(hk, "^", "Ctrl+")
    s := StrReplace(s, "!", "Alt+")
    s := StrReplace(s, "+", "Shift+")
    s := StrReplace(s, "#", "Win+")
    return StrUpper(s)
}
