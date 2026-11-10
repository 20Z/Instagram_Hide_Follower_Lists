# IG Shadow

A terminal tool that asks Instagram to hide the follower and following lists on your account.

-- by 2OZ

## How to use

1. Install Python 3.11 or newer. The Python that comes with macOS (3.9) is too old for password login; on a Mac, `brew install python@3.13` gives you `python3.13`. Then set up the project folder:

   ```bash
   python3.13 -m venv .venv
   source .venv/bin/activate
   python3 -m pip install -r requirements.txt
   ```

2. Run the script (in a new terminal, run `source .venv/bin/activate` first):

   ```bash
   python3 hide_lists.py
   ```

   **How do you want to log in?** Type `1` for **login** (username & password) or `2` for **session** (a `sessionid` cookie, see [below](#if-password-login-is-blocked)). You can also type the word.

3. For **login**, enter **your own** Instagram username and password when prompted. Your password will not appear on screen. If Instagram rejects it, you can type it again (3 tries).
4. If your account uses two-factor authentication, enter the code from your authenticator app, SMS or WhatsApp (3 tries). If Instagram asks you to confirm the login, enter the security code it sends you by SMS or email.
5. **What do you want to do?** Type `1` for **hide**, `2` for **unhide**, or `3` for **exit**. If Instagram confirms the change, a box shows `Enabled: Instagram returned value=true. Lists are hidden.` (or `Disabled: Instagram returned value=false. Lists are visible again.` for unhide). If something fails, a red box says what happened and what to try next.
6. The menu comes back after each change, so you can hide and unhide as often as you like without logging in again. Choose **exit** when you're done: the script logs out of the session it created.
7. Check your account from another account to see whether the lists are hidden. Example result:

   ![Example Instagram screen showing a hidden followers list](assets/result.jpeg)

Shortcuts: `--sessionid` picks session and `--unhide` unhides straight after logging in.

**Your password is sent only to Instagram.** The script keeps the login session in memory only while it runs and never saves it, or your password, to a file. Ctrl+C also logs out. If you close the terminal window instead, the script can't log out; you can end that session in Instagram's Login activity. Instagram may notify you about a new login from an Android device: that is this script.

## If password login is blocked

Instagram sometimes blocks logins from scripts or from some networks. You can use the `sessionid` cookie from a browser where you are already logged in instead (this works without installing the requirements):

1. Log in to **your own account** at [instagram.com](https://www.instagram.com/) in Chrome on a computer.
2. Open Developer Tools (`F12` or `Cmd+Option+I` on Mac). Go to **Application → Cookies → instagram.com**, then find the `sessionid` row and copy its **Value**.

   ![Where to find the sessionid cookie in Chrome Developer Tools; cookie values are hidden in this example](assets/find-sessionid.jpg)

3. Run the script, choose **session**, and paste **only the cookie value** when prompted (not `sessionid=`). Then choose **hide** or **unhide**:

   ```bash
   python3 hide_lists.py
   ```

**Keep your `sessionid` private.** It grants access to your account. Never share it or post a screenshot showing its value.

## Notes

- The output uses the red IG Shadow logo, borders and highlights in a terminal (a plain title in windows narrower than 76 columns). Set `NO_COLOR=1` to turn colours off.
- While Instagram is working (logging in, checking a code, changing the setting), a red spinner shows which step it is on and how many seconds have passed.
- Each Instagram request during login gives up after 20 seconds instead of waiting forever.
- Instagram sometimes answers a failed login with "Your version of Instagram is out of date", even when the real problem is the username or password. The script shows Instagram's actual reason when it can; otherwise, check that the login works in the Instagram app, then use `--sessionid`.
- With SMS two-factor, Instagram may text a new code when the script checks the one you typed. If the code is rejected, enter the newest one.
- This uses an unofficial Instagram endpoint and logs in the way the Instagram app does. It may not work for every account or may stop working. A success message means Instagram confirmed the setting, not that every viewer will see the same result. The settings request is never retried automatically: if it times out, check your account before running it again.
