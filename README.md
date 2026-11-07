# AMD Radeon R9700 Linux fan control

Ubuntu 26.04とAMD公式ドライバ・ROCmを前提とする、依存Pythonパッケージ不要のCLIです。
AMD SMIのC APIをPython標準の`ctypes`で呼び出してRPMを測定し、amdgpuのsysfsでファンを操作します。

**このPCのR9700で、40%への変更と自動制御への復帰を実機確認済みです。**
PWM 20%・約1,066 RPMからPWM 40%・約2,052 RPMへ変わり、
自動制御へ復帰後はPWM 20%・約1,064 RPMへ戻りました。

このPCでは`/etc/modprobe.d/90-amdgpu-fan-control.conf`の配置と、
kernel 7.0.0-38-genericのinitramfs更新、再起動まで完了しています。
動作中のマスクは`0xfff7ffff`で、`gpu_od/fan_ctrl/fan_curve`が公開されています。
再起動後にカーブを再適用するsystemdユニットも`deploy/`に同梱しています。
カーブはサスペンド復帰でも解除されるため、復帰時に再適用するユニットも入れてあります。

## 使い方

このディレクトリで実行します。Python 3.11以上と`uv`、AMD SMIの`libamd_smi.so`が必要です。

```bash
# 現在のRPM、温度、制御インターフェース
uv run fan_control.py status
uv run fan_control.py status --json

# 40%にして測定し、10秒後に標準の自動制御へ復帰
uv run fan_control.py test 40 --seconds 10

# 40%に固定。5秒待って変更後のRPMを測定し、設定を維持して終了
uv run fan_control.py set 40

# 標準の自動制御へ復帰
uv run fan_control.py auto

# 複数GPUの場合はDRM card名かPCI BDFを指定
uv run fan_control.py set 40 --gpu 0000:0b:00.0 --settle 8
```

## 温度カーブの設定

`curve`で「温度°C:PWM%」を指定できます。このR9700は**正確に5点**、
温度25〜100°C、PWM 20〜100%を受け付けます。点数・許容範囲は毎回ドライバから読みます。
温度は**hotspot / junction**で、`status`の`junction`を確認してください。
edge温度やメモリ温度を選ぶ機能はありません。

```bash
# 検証と内容表示だけ（sudo・書き込みなし）
uv run fan_control.py curve 25:20 50:45 65:65 85:85 100:100 --dry-run

# 適用して終了。以後はGPUファームウェアが温度に応じて制御
uv run fan_control.py curve 25:20 50:45 65:65 85:85 100:100

# TOMLから適用（上の指定と同じ設定）
uv run fan_control.py curve --config config/fan-curve.example.toml

# 一時的に設定して観測し、測定後10秒で標準自動制御へ戻す
uv run fan_control.py curve --config config/fan-curve.example.toml --settle 8 --seconds 10

# 現在のカーブ、PWM、RPM、温度
uv run fan_control.py status

# メーカー既定の自動制御へ戻す
uv run fan_control.py auto
```

TOMLの形式は次のとおりです。追加Pythonパッケージは不要です。

```toml
points = [
    [25, 20],
    [50, 45],
    [65, 65],
    [85, 85],
    [100, 100],
]
```

同梱のTOMLは2種類です。`config/fan-curve.example.toml`は`--config`の引数例、
`config/fan-curve.toml`は起動時ユニットが`/etc/r9700/fan-curve.toml`へインストールする
既定値で、このPCの実測値です。いずれも形式は同じで`points`キーだけを持ちます。

温度は重複なしの昇順、PWMは同じ値または増加する整数で指定します。
入力を勝手に並べ替えたり、点数を補間して増減したり、許容範囲へ丸めたりはしません。
点間の温度追従はGPUファームウェアの直線カーブ制御に任せます。
瞬時の温度表示とPWMが、温度の応答時間や量子化も含めて算術的に完全一致する保証ではありません。
低温側・高温側も指定で覆うよう、例では許容範囲の両端25°C・100°Cを含めています。

全点を検証してから書き込み、コミット後に全点を読み戻して照合します。
設定途中の失敗や測定中の中断時には標準自動制御への復帰を試みます。
`--seconds`なしの正常終了ではカーブが残り、プロセス終了後もGPU側が制御を続けます。
`--seconds`ありでは観測後に既定値へ戻します。以前の独自カーブへの復元ではありません。
再起動後にカーブを再適用する仕組みは次節「起動時にカーブを再適用する」の`deploy/`一式です。
カーブは再起動だけでなくサスペンド復帰でも解除されます。
カーブ設定は`fan_curve`対応GPU専用で、従来の`pwm1`のみのGPUには対応しません。

変更時だけ内部で`sudo -A /usr/bin/python3 ...`を実行します。uv自体をrootで動かす必要はありません。
認証は設定済みのaskpassヘルパーが行い、スクリプトはパスワードを扱いません。
NoNewPrivsでsudoが禁止されているサンドボックスでは、通常のホスト端末で実行してください。
制御非対応・範囲外の場合はsudoや書き込みを行う前にエラーで終了します。

`--settle`は変更後の測定までの待機時間です。`test`はその後さらに`--seconds`秒維持します。
`test`の正常終了、Ctrl+C、SIGTERMでは自動制御へ戻します。
`set`の正常終了後は手動設定が残るので、終了したいときは`auto`を実行します。
設定途中の失敗時も自動制御への復帰を試み、復帰失敗はエラーとして表示します。
SIGKILL・電源断での後処理はできません。常駐デーモンは同梱しません。
再起動とスリープ復帰後の再適用を図る起動時・復帰時ユニットを次節で提供します。

## 起動時にカーブを再適用する

カーブ設定は**再起動だけでなくサスペンド復帰でも解除される**ことが実機で確認できました。
`deploy/`の一式を組み込むと、起動時ユニットがログイン前の起動直後に、復帰時ユニットが
スリープからの復帰直後に、いずれも`/etc/r9700/fan-curve.toml`を再適用します。
常駐せず、両方とも`Type=oneshot`で必要な時だけ1回実行します。
`uv`は使わず、標準ライブラリのみで動く`/usr/bin/python3`でroot所有のコピーを実行します。

```bash
sudo -A deploy/install.sh
```

### 配置先と編集する場所

| パス | 内容 | 編集 |
| --- | --- | --- |
| `/etc/r9700/fan-curve.toml` | カーブ設定点。**随意に変更してよい唯一のファイル** | `sudoedit`で編集し`systemctl restart` |
| `/usr/local/lib/r9700-fan-control/fan_control.py` | このリポジトリの`fan_control.py`のroot所有コピー | 編集しない。更新時は再install |
| `/usr/local/sbin/r9700-fan-curve.sh` | 制御インターフェースの出現を待ち、上記を`--settle 0`で呼ぶ。両ユニットで共通 | 編集しない |
| `/etc/systemd/system/r9700-fan-curve.service` | 起動時。`WantedBy=multi-user.target` | 編集しない。上書きは`systemctl edit`のドロップイン |
| `/etc/systemd/system/r9700-fan-curve-resume.service` | スリープ復帰時。`WantedBy=suspend.target`ほか | 同上 |

`/usr/local`と`/etc`配下がすべてroot所有なのは、起動時ユニットがrootで実行されるためです。
`ExecStart=python3 /home/<user>/.../fan_control.py`のように、**書き換え可能なユーザー所有の
スクリプトをrootで実行させる形は一般的な権限昇格経路**なので使いません。リポジトリの作業
コピーを書き換えても削除しても、起動時の適用には影響しません。リポジトリを更新したときは
`sudo -A deploy/install.sh`でコピーを塗り直します。

### 確認と変更

```bash
systemctl status r9700-fan-curve.service        # 終了状態と直近ログ
journalctl -u r9700-fan-curve.service           # 適用内容の全文
uv run fan_control.py status                    # 現在のカーブ
sudoedit /etc/r9700/fan-curve.toml              # 設定変更
sudo -A systemctl restart r9700-fan-curve.service   # daemon-reloadは不要
```

復帰時ユニットの有効状態と、スリープ後に走ることは次のように確認します。

```bash
systemctl is-enabled r9700-fan-curve-resume.service        # enabled
systemctl list-dependencies --reverse suspend.target      # resume ユニットがぶら下がる
journalctl -u r9700-fan-curve-resume.service -b           # 復帰後の適用ログ
sudo -A systemctl suspend                                  # 眠らせて復帰させる
uv run fan_control.py status                               # 復帰後もカーブが残っていること
```

このPCの実測では`r9700-power-fan.service`がboot 16.9秒で完了し、GDM起動が17.2秒、
`graphical.target`到達が22.6秒です。ログイン操作を待たずに適用が終わります。

### 実機で確認した解除挙動

* **再起動**: 解除される。起動時ユニットが再適用（boot 61.4秒で`+255ms`、GDM起動より前）
* **サスペンド（S3）**: 解除される。amdgpuが復帰時に`MODE1 reset`でOverDriveファンテーブルを
  再初期化するためです。解除直後の読み戻しは全点ゼロ、`手動カーブ: 未設定（既定値）`となり
  実測PWMも 75.69% → 51.76%（junction 70°CでVBIOS既定の追従値）へ低下しました。hwmonの
  `power1_cap`（275 W）はそのまま保持され、`backend=fan_curve`も残るので
  `status`だけでは気づけません。
  **復帰時ユニットがこれを修復することを実測済みです**。`Waking up from system sleep state S3`
  と同一秒でユニットが走り、`変更前`が「未設定（既定値）」のまま`カーブ設定: 25°C:20% …`を
  書き戻して`status=0/SUCCESS`で完了しました（所要118ms、`RemainAfterExit=no`なので完了後
  `inactive`に戻り次の復帰でも再実行されます）
* **ハイバネーション**: このPCでは**実行不能**なので経路未確認。`/sys/kernel/security/lockdown`
  が`[integrity]`で、EFI Secure Boot由来のkernel lockdownがhibernationを禁止しています
  （`/sys/power/state`に`disk`が無く`/sys/power/disk`は`disabled`）。`WantedBy`へは
  念のため`hibernate.target`ほかにも張ってあります。ハイバネートからの復帰は
  userspaceの状態をそのまま復元するため起動時ユニットが再実行されず、この口がなければ
  カーブが消えたままになるからです。lockdownを解除する運用（Secure Boot無効化＋
  RAM以上の単一スワップ領域＋`resume=`設定とinitramfs更新）に変えた際の実測は未実施です

### 設計上の判断

* **待機**: amdgpuが初期化を済ませる前に`gpu_od/fan_ctrl/fan_curve`は現れません。既定で
  最大60秒、1秒間隔で存在を確認します。udevの`RUN+=`は「極めて短い同期タスクのみ」が規定で、
  実行中は同一デバイスの以降のイベントを止め、起動したプロセスはイベント終了後にkillされます。
  udevからsystemdを動かす場合も公式は`SYSTEMD_WANTS`によるサービス起動を推奨しています。
  systemdの`.device`ユニットに待機を委ねる手は、drm cardに`systemd`タグが無くunit名がsysfs
  パスのエスケープで長大になるため採用していません。
* **ブートを止めない**: `--settle 0`で変更後の測定を省略し、`TimeoutStartSec=180`で上限を
  切ります。`multi-user.target`に紐付くため、ユニットは完了まで起動トランザクションを
  待たせます。このPCでは数秒以内に完了しています。
* **失敗時**: `Restart=on-failure`と`StartLimitBurst`で無限リトライを防ぎます。
  `Type=oneshot`で許可されるのは`on-failure`だけで、`always`と`on-success`は拒否されます。
  設定値の不正は`fan_control.py`が書き込み前に拒否し、途中失敗時は自動制御への復帰を試みます。
* **ログイン後の仕組みを使わない理由**: `~/.profile`、GNOME自動起動、cron `@reboot`、
  systemd userユニットはいずれもログインかcron daemonの起動を待ち、root書き込みが必要なため
  `sudoers`のNOPASSWDや自動ログインが追加で必要になります。起動時ユニットなら昇格設定そのものが
  不要です。書き込み対象がroot限定のsysfs属性なので、udevやACLで非root化もできません。
* **複数GPU**: ファンを持つGPUが複数あると`fan_control.py`が対象を尋ねて終了します。
  `sudo -A systemctl edit r9700-fan-curve.service`で`Environment=GPU=0000:0b:00.0`を設定します。
  復帰時ユニットにも同じdrop-inを当ててください。
* **復帰時に別ユニットにする理由**: 起動時ユニットは`RemainAfterExit=yes`で`active`を
  維持します。systemdは`WantedBy=`で引っかけられた**すでにactiveのユニットを再startしない**
  ため、同じユニットに`suspend.target`を足しても復帰後に走りません。復帰時ユニットは
  `RemainAfterExit=no`にして毎回startされるようにします。
* **順序指定は`After=suspend.target`**とし`sleep.target`には張しません。`sleep.target`は
  眠る**前**にactiveになるため、`After=sleep.target`だと復帰後ではなくスリープ直前に
  走ります。実際の睡眠は`After=sleep.target`順で起動する`systemd-suspend.service`が担い、
  その`ExecStart`が復帰後に完結するので、`suspend.target`ほかの後段が復帰後になります。
  `/usr/lib/systemd/system-sleep/`のフックは公式が「hack」「Inhibitor Locksを使え」と
  明記しているため使いません。
* **睡眠に失敗したときも復帰時ユニットが走る**: 順序が「睡眠トランザクションの完結後」に
  一致するため、`pci_pm_prepare`がエラーで睡眠そのものが失敗した場合にもユニットは発火します。
  `fan_control.py`は冪等で、現在の値を読み戻して同じカーブを書き直すだけなので害はありません。
  部分睡眠でテーブルだけが消えた場合にも直るので、安全側の挙動です。
* **サスペンド自体が失敗することがある**: amdgpuの`pci_pm_prepare`が`-12`（ENOMEM）を返して
  `PM: Some devices failed to suspend`になる事例を実測しました（メモリ使用率が9割近く、
  `systemd-sleep`が`page allocation failure`を出した状態）。`deep`失敗後に`s2idle`でも
  同じ理由で失敗します。メモリを空ければ同じ操作で成功し、`/sys/power/suspend_stats/success`
  が加算されます。このCLIとユニットの動作とは無関係ですが、復帰検証をするときは負荷を下げて
  ください。

### 取り除く

```bash
sudo -A deploy/uninstall.sh
```

ユニット2本とroot所有のコピーを削除し、自動制御への復帰を試みます。
`/etc/r9700/fan-curve.toml`と`/etc/modprobe.d/90-amdgpu-fan-control.conf`は残ります。

## 「%」の意味

`set 40`は**ファンのPWM制御値40%**を意味します。
実測RPMが`fan1_max`の厳密な40%になるという保証はありません。
現機でもSMI制御値20%に対して、実測1,066 / 最大5,500 RPM ≒ 19.38%でした。
出力ではhwmonのPWM割合と、実測RPM / 最大RPMの割合を別々に表示します。
AMD SMIは実測RPMの取得に使います。このPCのOverDrive有効時は、AMD SMIの設定割合が
手動40%に変更しても0%のままでした。SMIの`level_percent`を実際のPWM割合として扱わず、
`hwmon_pwm.percent`を確認してください。`rocm-smi --showfan`でもPWM 40%を照合しています。
指定した最大RPM割合に厳密に合わせ続けるフィードバック制御は、この版には含みません。

## R9700の制御を有効化

設定案は[config/90-amdgpu-fan-control.conf](config/90-amdgpu-fan-control.conf)です。
このPCでは既に適用済みです。以下は他の環境や再セットアップ時の手順です。
元のマスクに`PP_OVERDRIVE_MASK`（bit 14、`0x4000`）だけを加えます。

```text
変更前: 0xfff7bfff
変更後: 0xfff7ffff
差分: 0x00004000
```

この設定は操作インターフェースを有効にします。それ自体ではクロック・電圧・ファン速度を変更しません。
他のPCやドライバ更新後はこのマスクをそのまま使わず、下記の手順で対象ドライバの既定値とビット定義を確認して計算してください。
既存の`options amdgpu`やカーネル引数に`ppfeaturemask`がある場合は競合を解消してください。

```bash
sudo -A install -m 0644 config/90-amdgpu-fan-control.conf /etc/modprobe.d/90-amdgpu-fan-control.conf
sudo -A update-initramfs -u -k all
```

作業を保存してPCを再起動した後に確認します。
実行中のGPUを使うプロセスに影響するため、モジュールの強制アンロードやPCI unbindは行いません。

```bash
cat /sys/module/amdgpu/parameters/ppfeaturemask
uv run fan_control.py status
uv run fan_control.py test 40 --seconds 10
amd-smi metric --gpu 0000:0b:00.0 --fan --temperature --json
```

`status`の`backend=fan_curve`が目安です。許容範囲はVBIOSから動的に読みます。
再起動後もインターフェースがない場合、マスクだけで解決できたとは判断できません。
`status --json`の診断とドライバのログを調査する必要があります。

設定を元に戻す場合は、まず`auto`を実行した上で、このファイルだけを削除しinitramfsを更新して再起動します。

```bash
uv run fan_control.py auto
sudo -A rm /etc/modprobe.d/90-amdgpu-fan-control.conf
sudo -A update-initramfs -u -k all
```

### マスク値を見直すタイミング

`/etc/modprobe.d/90-amdgpu-fan-control.conf`の値は、amdgpuカーネルドライバの
電力管理機能のマスクです。アップデートのたびに数値を変更する必要はありません。
**使用するドライバの既定マスク・ビット定義・機能の公開条件を確認し、変更がある場合に見直します。**

| 契機 | 確認すること |
| --- | --- |
| amdgpuドライバ更新 | 新版の既定マスク、OverDriveのビット定義、ファン制御の公開条件 |
| ROCm更新 | ユーザー空間の更新だけなら通常は変更不要。amdgpuも同時更新される場合はドライバ更新として確認 |
| Linuxカーネル更新 | 同じDKMSドライバの再ビルドか、異なるドライバ版・標準内蔵ドライバへの切り替えか |
| GPUファームウェア・VBIOS更新、GPU交換 | ファン制御インターフェースの有無、点数・温度・PWMの許容範囲。必ずマスク変更になるわけではない |
| OS更新、ドライバ導入方法・起動設定の変更 | 使用ドライバ、設定ファイルの保持、他の設定との競合、initramfsへの収録 |

この設定はマスク全体を数値で上書きします。
「その時点の既定値にOverDriveだけを追加する」という指定をmodprobeが毎回計算するわけではありません。
そのため、カーブが引き続き動いていても、古い固定値が新しいドライバの既定方針とずれていないかを確認します。

### 参照元と対象ドライバの特定

参照する一次情報は次の3か所です。GitHubの最新版だけでなく、**実際に使用するドライバ版のソース**を基準にします。

| 情報 | ソース内の場所・識別子 | このPCで確認した値・動作 |
| --- | --- | --- |
| ビットの意味 | `amd/include/amd_shared.h`の`enum PP_FEATURE_MASK`、`PP_OVERDRIVE_MASK` | `0x4000`（bit 14） |
| 設定未指定時の既定マスク | `amd/amdgpu/amdgpu_drv.c`の`amdgpu_pp_feature_mask`初期値 | `0xfff7bfff` |
| OverDriveの有効化判定 | `amd/pm/swsmu/amdgpu_smu.c`の`smu_set_funcs`、`od_enabled` | `PP_OVERDRIVE_MASK`が立っているかを判定 |

上表の相対パスは、このPCのAMD DKMSソースを基準にしています。
Linux本体のソースでは`amd/`部分が`drivers/gpu/drm/amd/`です。
[カーネルのパラメータ説明](https://docs.kernel.org/gpu/amdgpu/module-parameters.html)も
`enum PP_FEATURE_MASK`を定義元として案内しています。
公開ソースの参照先は[ビット定義](https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/amd/include/amd_shared.h)、
[既定値](https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/amd/amdgpu/amdgpu_drv.c)、
[有効化判定](https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/amd/pm/swsmu/amdgpu_smu.c)です。
これらの`master`リンクは構造を調べるための参照先で、導入済み版と一致する保証はありません。

まず、動作中とディスク上のドライバを区別して確認します。

```bash
uname -r
cat /sys/module/amdgpu/version
modinfo -n amdgpu
modinfo -F version amdgpu
dkms status
```

このPCでは`modinfo -n amdgpu`が
`/lib/modules/7.0.0-38-generic/updates/dkms/amdgpu.ko.zst`を返し、
AMDのDKMS版が選ばれていました。`modinfo`はディスク上のモジュール情報なので、
更新直後・再起動前には動作中の版と異なる場合があります。
次回起動するカーネルが別なら、`modinfo -k <対象カーネル版> -n amdgpu`と
`modinfo -k <対象カーネル版> -F version amdgpu`でその版を確認します。
バージョン情報が出ない場合も、モジュールのパスと導入パッケージに対応するソースを確認してください。

次のソースパスは初回検証時の例です。更新後は対象モジュールに対応するパッケージのソースへ置き換えます。
`/usr/src`に旧版が残っていても、その旧版の定義を新版の計算に使わないでください。
対応するソースが手元にない場合は、導入元が提供する該当版のソースを参照します。

```bash
amdgpu_source=/usr/src/amdgpu-7.1.3-2390945.26.04
rg -n 'amdgpu_pp_feature_mask[[:space:]]*=' "$amdgpu_source/amd/amdgpu/amdgpu_drv.c"
rg -n 'PP_OVERDRIVE_MASK[[:space:]]*=' "$amdgpu_source/amd/include/amd_shared.h"
rg -n -A 4 -B 4 'smu->od_enabled = true' "$amdgpu_source/amd/pm/swsmu/amdgpu_smu.c"
```

`cat /sys/module/amdgpu/parameters/ppfeaturemask`で読めるのは、設定を上書きした後の動作中の値です。
そこから**新しいドライバ本来の既定値は取得できません**。既定値の確認には上記ソースを使います。

### マスクの導出

基本式は`対象ドライバの既定マスク | 対象ドライバのPP_OVERDRIVE_MASK`です。
既に他のビットを意図的に変更している場合は、その目的も確認して基準マスクに反映します。
古い設定値を無条件に基準にすると、新しい既定値との差分を見落とします。

初回検証では次の定義を確認しました。

```c
/* amd/amdgpu/amdgpu_drv.c */
uint amdgpu_pp_feature_mask = 0xfff7bfff;

/* amd/include/amd_shared.h */
PP_OVERDRIVE_MASK = 0x4000,
```

確認した値を使ってPythonで計算できます。以下は計算と表示だけで、設定は変更しません。
ドライバ更新後は、2つの入力値をその版の定義に合わせて置き換えてください。

```bash
python3 - <<'PY'
default_mask = int("0xfff7bfff", 16)
overdrive_mask = int("0x4000", 16)
new_mask = default_mask | overdrive_mask
print(f"既定値: {default_mask:#010x}")
print(f"追加ビット: {overdrive_mask:#010x}")
print(f"設定値: {new_mask:#010x}")
print(f"変更されたビット: {default_mask ^ new_mask:#010x}")
PY
```

この例の設定値は`0xfff7ffff`、変更されたビットは`0x00004000`です。
もともと無効なGFX DCS（bit 19）は無効のままです。
既定値で既にOverDriveが有効なら、この目的のための上書き設定は不要か検討できます。
「全ビットを1にする」値へ置き換える方法は使いません。

### 設定の反映と確認

数値を変更する前に、既存のmodprobe設定と起動引数を確認します。
`modprobe -c`は現在の設定ファイルから組み立てた内容で、既に動作中のGPUへの反映確認ではありません。

```bash
modprobe -c | rg '^options amdgpu'
rg -n 'ppfeaturemask' /etc/modprobe.d /usr/lib/modprobe.d /etc/default/grub /etc/default/grub.d /proc/cmdline 2>/dev/null
```

別ファイルや起動引数に重複指定があれば解消します。
導出した数値を`config/90-amdgpu-fan-control.conf`に記載し、コメントにも対象ドライバ版・
既定値・追加したビットの根拠を残してから配置します。

```bash
sudo -A install -m 0644 config/90-amdgpu-fan-control.conf /etc/modprobe.d/90-amdgpu-fan-control.conf
sudo -A update-initramfs -u -k all
```

このPCの`mkinitramfs`は`/etc/modprobe.d/*.conf`をinitramfsへコピーします。
そのため、**マスク値の見直し**と**変更した設定のinitramfsへの反映**は別の作業です。
カーネル更新時のinitramfs生成では通常、既存の設定ファイルも取り込まれます。
設定ファイルを変更した場合は、起動に使うinitramfsを更新します。
`-u -k all`は既存のinitramfsがある全カーネル版を対象にします。
特定版だけを更新する場合は`-k <対象カーネル版>`を使います。
[update-initramfsの仕様](https://manpages.debian.org/trixie/initramfs-tools/update-initramfs.8.en.html)も参照してください。

更新が成功したことと、次回起動に使うイメージへの収録を確認します。
次はkernel 7.0.0-38-genericの例なので、確認対象の版に合わせてパスを置き換えます。

```bash
cat /etc/modprobe.d/90-amdgpu-fan-control.conf
modprobe -c | rg '^options amdgpu'
lsinitramfs /boot/initrd.img-7.0.0-38-generic | rg 'etc/modprobe.d/90-amdgpu-fan-control.conf'
```

再起動後に、動作中の版・実効マスク・制御インターフェースを確認します。

```bash
uname -r
cat /sys/module/amdgpu/version
cat /sys/module/amdgpu/parameters/ppfeaturemask
uv run fan_control.py status --json
uv run fan_control.py curve --config config/fan-curve.example.toml --dry-run
```

実効マスクが意図した値であること、`backend`が`fan_curve`であること、
新しい温度・PWM許容範囲や点数でも設定例が妥当かを確認します。
値が反映されてもインターフェースが出ない場合は、ドライバ・GPUファームウェア・VBIOSの
公開条件を調べます。マスクをさらに広げれば解決するとは限りません。
カーブそのものは再起動で解除されるため、起動設定の反映確認後に必要に応じて再適用します。

## 制御方式

* R9700などの`gpu_od/fan_ctrl/fan_curve`がある場合: 全ての公開設定点のPWMを同じ割合にしてコミットします。有効な温度設定がある場合は維持し、初期値が全点ゼロの場合は公開された許容温度範囲に設定点を配置します。このPCでは25、44、63、81、100°Cです。Zero RPM設定が対応している場合は停止機能を無効化します。空欄の非対応項目には書き込みません。設定を読み戻して一致を確認します。
* 従来の書き込み可能な`pwm1`と`pwm1_enable`がある場合: 手動モードにして、`pwm1_max`に対する割合を丸めて設定します。
* `auto`: ファンカーブと変更したZero RPM項目をVBIOS既定値に戻すか、従来の自動モードに戻します。以前の独自ファン設定を保存・復元する機能ではありません。

新しいAMD SMIの`set --fan`は`gpu_od`環境で`fan_minimum_pwm`を使う場合があります。
これは自動制御中の下限で、一定割合の固定設定と同じではありません。そのため、このCLIでは固定カーブを使います。
ファームウェアの保護動作によって実際のファン動作が変わる場合があります。
`set`による固定割合では温度に追従しないため、負荷に対して十分な割合を選び、測定値を確認してください。
複数の制御ソフトを同時に操作すると設定が競合します。

## 検証

```bash
uv run --offline python -m unittest discover -s tests -v
```

入力範囲、書き込み非対応、複数GPUの曖昧さ、設定の読み戻し、途中失敗時の復帰などを検証します。
37件が成功しています。カーブ入力の点数・範囲・並び順・TOML、書き込み前の拒否、
dry-run時の非昇格、一時設定の自動復帰、初期カーブが全点ゼロの場合、空欄のZero RPM項目、
SMI初期化が失敗した場合の自動制御復帰も確認しています。
`tests/test_deploy.py`はハードウェアに依存せず、起動スクリプトの引数組み立てと
インターフェース未出現時の取り直し、unit・install・uninstallが参照するパスの一致、
起動時ユニットが`multi-user.target`のみで1回だけ走ることを確認します。
復帰時ユニットには`sleep.target`順序指定の誤り検出と`RemainAfterExit=no`の検査も入れています。
ファイルを使うテストと実機のファン変更は別で、テスト合格だけでは実機での制御成功を意味しません。
再起動前後の実測は[validation/2026-10-23.md](validation/2026-10-23.md)に記録しています。
温度カーブのTOML/CLI設定、実測PWM/RPM、コマンド終了後の保持確認は
[validation/2026-10-23-curve.md](validation/2026-10-23-curve.md)に記録しています。

`deploy/`の一式は実機に組み込み済みです。起動時ユニットは再起動を跨いで適用されること、
サスペンド復帰でカーブが解除されること、復帰時ユニットがその解除を修復することを
いずれも実測しています（上の節）。**ハイバネーション復帰の経路のみ未実測です**
（このPCではkernel lockdownで実行不能なため）。

## 他のAMD Radeon製品での利用

**実機で動作確認済みなのは、このPCのRadeon AI PRO R9700です。**
同じamdgpuインターフェースを公開する他製品でも利用できる可能性はありますが、
Radeon製品であることや、`ppfeaturemask`を調整したことだけで動作を保証するものではありません。
対応可否は、ドライバ・GPUファームウェア・VBIOSが公開する制御機能によって決まります。

| 公開されている制御インターフェース | 現在の実装で利用できる範囲 |
| --- | --- |
| 今回と同じ形式の`gpu_od/fan_ctrl/fan_curve` | 固定PWM・温度カーブともに対応候補。別製品での実機検証は未実施 |
| 書き込み可能な`pwm1`と`pwm1_enable` | 固定PWM・自動復帰の経路を実装済み。模擬テストのみで、`curve`は非対応 |
| ファン制御がamdgpuに公開されない構成 | 非対応。別コントローラで冷却ファンを管理する構成などは別途対応が必要 |
| 古い`radeon`ドライバを使う構成 | 非対応。デバイス検出は`amdgpu`を前提としている |

カーブの点数・温度範囲・PWM範囲はドライバから取得するため、
R9700の「5点・25〜100°C・20〜100%」を他製品へ固定して適用する実装ではありません。
ただし、現状はカーブのパスとテキスト形式を今回確認したものに合わせており、
形式が異なるGPUには追加対応が必要です。
また、測定時に`fan1_input`・`fan1_max`などを必須としているため、
制御インターフェースがあっても、これらの項目が存在しない・読み取れないGPUではエラーになる場合があります。

他製品で確認する場合は、次の順序で進めます。

1. 対象ドライバの定義を調べ、必要なら前述の手順でマスクを導出・反映する。
2. `uv run fan_control.py status --json`で、対象GPU・`backend`・センサー値・カーブの形式と許容範囲を確認する。
3. `fan_curve`対応なら、そのGPUの点数・許容範囲に合った設定を`curve ... --dry-run`で検証する。付属TOMLはR9700用の例なので、そのまま流用できるとは限らない。
4. 固定PWMは`test`、カーブは`curve ... --seconds 10`で一時適用し、設定の読み戻し、PWM/RPMの変化、標準自動制御への復帰を実測する。複数GPUでは`--gpu`で対象を明示する。

`status`でファイルが見えることや`dry-run`が成功することだけでは、
GPUが実際に設定を受け付け、意図したファン動作になる保証にはなりません。
互換性の判断は製品シリーズ名だけで行わず、制御・測定・復帰まで確認してください。

## 参照

* [AMD SMI Python API / ファン速度取得・設定](https://rocm.docs.amd.com/projects/amdsmi/en/latest/reference/amdsmi-py-api.html)
* [systemd.service(5) — Type=oneshotとRestart=の制約](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)
* [udev(7) — RUN+=の制約とSYSTEMD_WANTSによるサービス起動](https://www.freedesktop.org/software/systemd/man/latest/udev.html)
* [systemd.device(5) — `TAG+="systemd"`とデバイスユニット](https://www.freedesktop.org/software/systemd/man/latest/systemd.device.html)
* [Linux amdgpu: HWMON・fan_curve・fan_minimum_pwm](https://docs.kernel.org/gpu/amdgpu/thermal.html)
* [AMD SMI公式実装](https://github.com/ROCm/amdsmi)
* [amdgpu SMU 14.0.2の実装](https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/amd/pm/swsmu/smu14/smu_v14_0_2_ppt.c)

このPCのインストール済みソースも照合しました。
`/usr/src/amdgpu-7.1.3-2390945.26.04/amd/include/amd_shared.h`の`PP_OVERDRIVE_MASK = 0x4000`、
`amd/pm/swsmu/amdgpu_smu.c`の`od_enabled`、
`amd/pm/swsmu/smu14/smu_v14_0_2_ppt.c`のカーブ編集・復帰処理、
`/opt/rocm/core-10.0/include/amd_smi/amdsmi.h`のC APIとBDFレイアウトを使っています。
