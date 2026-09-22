# 腾讯云 COS 上机手册（私有桶 + 最小权限子账号 + coscli）

向导第 5 步的详细版；判据的口径在[三机上机清单](三机上机清单.md) §二 第 10 步与 §四 第 3 步。
这套东西干三件事：一只**私有桶**存图片（key 形如 `img/<哈希前2位>/<哈希>.<扩展名>`）、
一把**子账号密钥**只够这个桶用、一个 **coscli** 让程序能存取（凭据在 `~/.cos.yaml`，不进代码仓）。

两种档位，先认准本机是哪一档（`config.toml` 的 `machine.cos_access`）：

| 档位 | 谁 | 策略给什么 |
|---|---|---|
| `readwrite` | 采集机 | 桶下 `img/*` 的**读 + 写 + 删**（删只为清验证对象；程序自身只写不删） |
| `read_only` | 纯汇总机 | 桶下 `img/*` 的**只读**（Get / Head / List） |

---

## 一、建私有桶

控制台 → 对象存储 → 存储桶列表 → **创建存储桶**：

1. **名称**：小写字母、数字、连字符，如 `bestseller-exchange`。控制台会自动追加 `-<APPID>`，
   最终桶名形如 `bestseller-exchange-1250000000`——后面到处都用这个**完整桶名**。
2. **地域**：就近选（如广州 `ap-guangzhou`）。记住它，策略和 coscli 都要写。
3. **访问权限**：**私有读写**（默认值）——千万别选公共读，桶里是真实经营数据。
4. 其余默认，创建。

判据：桶列表里能看到，访问权限那列是「私有」。

## 二、建子账号与自定义策略（最小权限）

访问管理 CAM → 用户 → 用户列表 → **新建用户** → **自定义创建**：

1. **访问方式**：勾选**编程访问**（会生成 SecretId / SecretKey，**只在创建时完整显示**，
   复制到记事本存好）。
2. **设置用户策略**：**不要**勾任何预设的 `QcloudCOSDataFullControl` 之类——预设的桶级全读写
   会盖过下面的细粒度授权。点「新建自定义策略」→「按策略语法」，贴：

   **采集机档**（把 `ap-guangzhou`、`1250000000`、桶名换成你自己的）：

   ```json
   {
     "version": "2.0",
     "statement": [
       {
         "effect": "allow",
         "action": ["cos:GetObject", "cos:HeadObject", "cos:PutObject", "cos:DeleteObject"],
         "resource": ["qcs::cos:ap-guangzhou:uid/1250000000:bestseller-exchange-1250000000/img/*"]
       }
     ]
   }
   ```

   **纯汇总机档**：把 `cos:PutObject`、`cos:DeleteObject` 去掉，只留 `cos:GetObject`、
   `cos:HeadObject`。

   资源写法说明：`qcs::cos:<地域>:uid/<主账号APPID>:<桶名-APPID>/<前缀>*`——`uid/` 后面是
   主账号 APPID，冒号后面是完整桶名，最后是要授权的对象前缀；`img/*` 就是「只到 img/ 这层」。
   **地域可以留空**（写成 `qcs::cos::uid/…`）= 不限地域，省得地域填错；要收紧再填具体地域。
   **列目录**要单独的**桶级**动作（`cos:HeadBucket`、`cos:GetBucket`，资源给 `…/<桶名-APPID>/*`），
   它们没法只限 `img/` 前缀——所以策略通常写两条 statement：桶级一条（列目录）、对象级一条（读写）。

   三个占位符从哪来：

   | 占位符 | 是什么 | 去哪看 |
   |---|---|---|
   | `ap-guangzhou` | 桶的**地域** | 建桶时选的那个；桶列表/概览里也写着（广州 `ap-guangzhou`、上海 `ap-shanghai`、北京 `ap-beijing`、成都 `ap-chengdu`） |
   | `1250000000` | 主账号 **APPID**（一串数字） | **从完整桶名里直接读**：`名字-APPID` 横杠后面那串；也可在控制台右上角账号信息里看 |
   | `bestseller-exchange-1250000000` | **完整桶名**（带 `-APPID` 后缀） | 桶列表里的名字（不是建桶表单里填的短名） |
3. 策略建好后**挂到这个用户上**，完成，复制 SecretId / SecretKey。

判据：用户列表里有这个子账号；它的策略里资源写着 `img/*`。

## 三、装 coscli 并配置

1. 从 [coscli 的 release](https://github.com/tencentyun/coscli/releases) 下 Windows 版 exe
   （资产名形如 `coscli-v<版本>-windows-amd64.exe`），**改名成 `coscli.exe`**，放进一个
   **已经在 PATH 上的目录**。

   本机现成的落点：`C:\Users\Darwin\.local\bin`（用户变量 `Path` 的第一条，`uv.exe`、`python3.12.exe`
   都在那儿）。**放进去就行，不用改任何设置**，当前开着的 Git Bash 窗口也能立刻用——PATH 目录没变，
   只是目录里多了个文件（只有改 PATH 本身才需要重开窗口）。一行搞定：

   ```bash
   curl -L -o "/c/Users/Darwin/.local/bin/coscli.exe" \
     https://github.com/tencentyun/coscli/releases/download/v1.0.9/coscli-v1.0.9-windows-amd64.exe
   ```

   要在别的机器上**新建**一个目录：Win 键搜「环境变量」→「编辑账户的环境变量」→ 用户变量里的
   `Path` → 新建一行。**别用 `setx PATH "%PATH%;新目录"`**——它会把用户与系统 PATH 合并并在
   1024 字符处截断，很容易把 Path 弄坏。

   判据：`coscli --version` 有输出。
2. 交互式生成配置（写进 `~/.cos.yaml`）：

   ```bash
   coscli config init
   ```

   它按顺序问下面这些（版本不同措辞可能略变，认关键词即可）：

   | 提示（关键词） | 填什么 |
   |---|---|
   | Secret ID | 子账号那把 `AKID…` |
   | Secret Key | 子账号那把（与 SecretId 配对） |
   | Session Token | **直接回车**（没有临时密钥就留空） |
   | Mode / Cvm Role Name | **直接回车**（默认 SecretKey） |
   | Auto Switch Host | 直接回车 |
   | **APPID** | 账号 APPID——**不是主账号 ID（UIN，形如 `1000xxxxxx`）**；从完整桶名横杠后面读：`bestseller-exchange-1250000000` 里的 `1250000000` |
   | **Bucket Name** | **完整桶名**（`<名字>-<APPID>`），不是建桶表单里填的短名 |
   | **Bucket Endpoint** | `cos.<地域>.myqcloud.com`（广州 = `cos.ap-guangzhou.myqcloud.com`） |
   | Bucket Alias / OFS Bucket | 回车跳过即可（别名只是省打字） |

   密钥在 `~/.cos.yaml` 里是**加密存储**的；想手工改这个文件得先关掉加密，所以换密钥优先重跑
   `config init` 或 `coscli config set --secret_id … --secret_key …`。
3. 加桶（`-b` 完整桶名、`-r` 地域、`-e` 是 `cos.<地域>.myqcloud.com`，就是上表那批值；
   `-a` 是别名、可省）：

   ```bash
   coscli config add -b bestseller-exchange-1250000000 -r ap-guangzhou -e cos.ap-guangzhou.myqcloud.com
   ```

4. 看一眼结果：

   ```bash
   coscli config show
   ```

   密钥就在 `~/.cos.yaml` 里——**别放进代码仓，也别贴进日志**。

判据：`coscli ls -r cos://bestseller-exchange-1250000000/img/` 能列出（空桶也算成功）。

## 四、自证（向导第 5 步会自动跑，也可以自己跑）

**采集机档**：

```bash
echo hello > /tmp/t.txt && coscli cp /tmp/t.txt cos://<桶名>/img/wizard-check.txt   # 传得上
coscli cp cos://<桶名>/img/wizard-check.txt /tmp/back.txt                          # 取得回
coscli rm cos://<桶名>/img/wizard-check.txt                                        # 删得掉
coscli ls -r cos://<桶名>/wizard-check-not-img/                                    # img/ 以外：应被拒
```

**纯汇总机档**：上传那一步**应该失败**（被拒 = 只读档位正确）；下载已知对象要成功。

## 五、故障对照

| 症状 | 多半是什么 |
|---|---|
| `AccessDenied` / 403 | 策略没挂上子账号；或动作名写错；或桶名/APPID 写错 |
| 能**上传**，但**下载 / 列目录**都被拒 | 策略里只给了写动作。补对象级 `cos:GetObject`、`cos:HeadObject`（资源 `…/img/*`）与桶级 `cos:HeadBucket`、`cos:GetBucket`（资源 `…/<桶名-APPID>/*`）——照第二节的两条 statement 写全 |
| 列目录报 403，但能下载已知对象 | 策略里缺**桶级**列目录动作。加一条 statement：`action: ["cos:HeadBucket","cos:GetBucket"]`、`resource: ["qcs::cos:<地域>:uid/<APPID>:<桶名-APPID>/*"]`。注意桶级动作能看到桶内全部 key（含 img/ 以外）——这是动作本身的范围，**写权限仍限在 img/* 那条**即可 |
| 整个 `coscli ls`（不带桶）报权限错 | 那是拉桶列表（`GetService`，只能授权给 `*`）。本项目用不到，忽略 |
| 404 / `NoSuchBucket` | 桶名少了 `-APPID`，或地域填错 |
| `找不到 coscli` | exe 没进 PATH；向导第 5 步也会点这句 |
| 密钥想换一把 | `coscli config init` 重跑一遍（它会重写 `~/.cos.yaml`），或 `coscli config set --secret_id … --secret_key …` |
