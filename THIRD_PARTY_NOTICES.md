# Third-party components

The root MIT license covers the application code written for this repository.
Third-party packages and runtimes retain their respective licenses.

| Component | Version used | License / source |
| --- | --- | --- |
| NeteaseCloudMusicApiEnhanced | 4.41.1 | MIT; https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced |
| Node.js runtime used for the Windows build | 26.5.1 | See `docs/NODE-LICENSE.txt`, including bundled-component notices |
| Python runtime used for the Windows build | 3.12.10 | See `docs/PYTHON-LICENSE.txt` |
| PyInstaller build tooling | 6.22.3 | https://pyinstaller.org/en/stable/license.html |
| Optional Selenium WebDriver support | 4.x | Apache-2.0; https://github.com/SeleniumHQ/selenium |

The API's transitive dependency versions and package metadata are recorded in
`api-server/package-lock.json`. Source distributions do not vendor `node_modules`.
The Windows build includes package files and their original license notices.

NetEase Cloud Music and Microsoft Edge names identify the services and browser
with which this application interoperates. Their trademarks remain with their owners.
