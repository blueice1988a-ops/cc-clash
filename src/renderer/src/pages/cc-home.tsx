import { Button, Card, CardBody, Input, Switch } from '@heroui/react'
import React, { useEffect, useState } from 'react'
import { toast } from '@renderer/components/base/toast'
import { useAppConfig } from '@renderer/hooks/use-app-config'
import { useControledMihomoConfig } from '@renderer/hooks/use-controled-mihomo-config'
import { useProfileConfig } from '@renderer/hooks/use-profile-config'
import { calcTraffic } from '@renderer/utils/calc'
import { platform } from '@renderer/utils/init'
import {
  addProfileItem,
  changeCurrentProfile,
  checkAutoRun,
  checkMihomoCorePermissions,
  enableAutoRun,
  getControledMihomoConfig,
  getFilePath,
  getProfileConfig,
  grantTunPermissions,
  mihomoVersion,
  patchAppConfig,
  patchControledMihomoConfig,
  readTextFile,
  removeProfileItem,
  restartAsAdmin,
  restartCore,
  setOperationMode,
  showTunPermissionDialog,
  triggerSysProxy,
  updateTrayIconImmediate
} from '@renderer/utils/ipc'
import { markInitialContentPartReady } from '@renderer/utils/startup'
import ccIcon from '@renderer/assets/cc-icon.png'

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

// 确认代理核心还在运行，避免配置没生效却显示已连接
async function ensureCoreRunning(): Promise<void> {
  try {
    await mihomoVersion()
  } catch (e) {
    throw new Error(`代理核心没有正常运行：${errorText(e)}`)
  }
}

// 打开虚拟网卡，返回是否真的打开了
async function enableTun(): Promise<boolean> {
  if (!(await checkMihomoCorePermissions())) {
    if (platform === 'win32') {
      // 重启成管理员后，主进程会自动打开虚拟网卡
      if (await showTunPermissionDialog()) await restartAsAdmin()
      return false
    }
    try {
      await grantTunPermissions()
    } catch {
      return false
    }
    if (!(await checkMihomoCorePermissions())) return false
  }
  await patchControledMihomoConfig({ tun: { enable: true }, dns: { enable: true } })
  return (await getControledMihomoConfig(true)).tun?.enable === true
}

// 打开系统代理和虚拟网卡，返回虚拟网卡是否打开
async function connect(): Promise<boolean> {
  if ((await getControledMihomoConfig(true)).mode !== 'rule') {
    await patchControledMihomoConfig({ mode: 'rule' })
  }
  await patchAppConfig({ sysProxy: { enable: true } })
  try {
    await triggerSysProxy(true)
  } catch (e) {
    await patchAppConfig({ sysProxy: { enable: false } })
    throw new Error(`系统代理没有打开：${errorText(e)}`)
  }
  const tun = await enableTun()
  await ensureCoreRunning()
  updateTrayIconImmediate(true, tun)
  return tun
}

async function disconnect(): Promise<void> {
  await patchAppConfig({ sysProxy: { enable: false } })
  await triggerSysProxy(false)
  if ((await getControledMihomoConfig(true)).tun?.enable) {
    await patchControledMihomoConfig({ tun: { enable: false } })
  }
  updateTrayIconImmediate(false, false)
}

// 新线路用不了时，删掉这次新加的线路，换回原来的线路
async function restoreProfiles(previous: IProfileConfig): Promise<void> {
  const before = new Set(previous.items.map((i) => i.id))
  const now = await getProfileConfig(true)
  for (const item of now.items) {
    if (!before.has(item.id)) await removeProfileItem(item.id).catch(() => {})
  }
  const after = await getProfileConfig(true)
  if (previous.current && after.current !== previous.current) {
    await changeCurrentProfile(previous.current).catch(() => {})
  } else {
    await restartCore().catch(() => {})
  }
}

// Cc clash 的唯一界面：导入一条链接，然后自动打开系统代理和虚拟网卡
const CcHome: React.FC = () => {
  const { appConfig, mutateAppConfig } = useAppConfig()
  const { controledMihomoConfig, mutateControledMihomoConfig } = useControledMihomoConfig()
  const { profileConfig, mutateProfileConfig } = useProfileConfig()
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)

  const sysProxyOn = appConfig?.sysProxy?.enable ?? false
  const tunOn = controledMihomoConfig?.tun?.enable ?? false
  const connected = sysProxyOn || tunOn
  const current = profileConfig?.items.find((item) => item.id === profileConfig.current)
  const used = current?.extra ? current.extra.upload + current.extra.download : undefined

  useEffect(() => {
    markInitialContentPartReady('route')
    markInitialContentPartReady('sider')
  }, [])

  // 选过“简易模式”的旧安装会改写服务器下发的规则，这里统一切回标准模式
  useEffect(() => {
    if (!appConfig) return
    if (appConfig.operationMode === 'standard' && appConfig.modeSelected) return
    setOperationMode('standard').catch((e) => toast.error(errorText(e)))
  }, [appConfig])

  const run = async (action: () => Promise<void>): Promise<void> => {
    if (busy) return
    setBusy(true)
    try {
      await action()
    } catch (e) {
      toast.error(errorText(e))
    } finally {
      setBusy(false)
      mutateAppConfig()
      mutateControledMihomoConfig()
      mutateProfileConfig()
      window.electron.ipcRenderer.send('updateFloatingWindow')
      window.electron.ipcRenderer.send('updateTrayMenu')
    }
  }

  const connectNow = (): Promise<void> =>
    run(async () => {
      if (!(await connect())) toast.warning('已连上，但虚拟网卡没有打开，部分软件可能不走代理')
    })

  const turnOnTun = async (): Promise<void> => {
    if (!(await enableTun())) {
      // Windows 会先弹窗重启成管理员，其他系统是没拿到权限
      if (platform !== 'win32') throw new Error('没有拿到权限，虚拟网卡没有打开')
      return
    }
    await ensureCoreRunning()
    updateTrayIconImmediate(sysProxyOn, true)
  }

  const turnOffTun = async (): Promise<void> => {
    await patchControledMihomoConfig({ tun: { enable: false } })
    updateTrayIconImmediate(sysProxyOn, false)
  }

  // 新线路连上之后才删除旧线路；连不上就恢复原来的线路
  const importProfile = async (item: Partial<IProfileItem>): Promise<void> => {
    const previous = await getProfileConfig(true)
    const before = new Set(previous.items.map((i) => i.id))
    let addedId: string
    let tun: boolean
    try {
      await addProfileItem(item)
      const config = await getProfileConfig(true)
      const added =
        config.items.find((i) => !before.has(i.id)) ??
        config.items.find((i) => item.url && i.url === item.url)
      if (!added) throw new Error('没有读到线路，请检查链接是否正确')
      addedId = added.id
      if (config.current !== added.id) await changeCurrentProfile(added.id)
      await ensureCoreRunning()
      tun = await connect()
    } catch (e) {
      await restoreProfiles(previous)
      const kept = previous.items.length > 0 ? '，原来的线路没有变' : ''
      throw new Error(`导入失败${kept}：${errorText(e)}`)
    }
    for (const old of (await getProfileConfig(true)).items) {
      if (old.id !== addedId) await removeProfileItem(old.id).catch(() => {})
    }
    try {
      if (!(await checkAutoRun())) await enableAutoRun()
    } catch {
      // 开机自启失败不影响使用
    }
    setUrl('')
    if (tun) toast.success('导入成功，已经连上了')
    else toast.warning('导入成功，已连上，但虚拟网卡没有打开，部分软件可能不走代理')
  }

  const importLink = (): Promise<void> =>
    run(async () => {
      const link = url.trim()
      if (!/^https?:\/\//i.test(link)) throw new Error('请粘贴以 http 开头的链接')
      await importProfile({ name: '', type: 'remote', url: link })
    })

  const importFile = (): Promise<void> =>
    run(async () => {
      const files = await getFilePath(['yml', 'yaml'])
      if (!files?.length) return
      const content = await readTextFile(files[0])
      const name = files[0].split(/[\\/]/).pop() || 'Cc clash'
      await importProfile({ name, type: 'local', file: content })
    })

  return (
    <div className="h-screen w-full flex flex-col bg-background">
      <div className="app-drag h-12 shrink-0" />
      <div className="flex-1 overflow-y-auto flex flex-col items-center px-8 pb-10">
        <img src={ccIcon} alt="" className="h-16 w-16 shrink-0" />
        <h1 className="text-2xl font-bold mt-2">Cc clash</h1>

        <Card className="w-full max-w-md mt-6 shrink-0">
          <CardBody className="flex flex-col gap-4 p-5">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span
                  className={`h-2.5 w-2.5 rounded-full ${connected ? 'bg-success' : 'bg-default-400'}`}
                />
                <span className="text-lg font-semibold">
                  {!current ? '还没有导入线路' : connected ? '已连接' : '未连接'}
                </span>
              </div>
              {current && (
                <p className="text-small text-default-500 mt-1 truncate">
                  {current.name}
                  {used !== undefined && ` · 本月已用 ${calcTraffic(used)}`}
                </p>
              )}
            </div>
            <Button
              size="lg"
              color={connected ? 'default' : 'primary'}
              variant={connected ? 'flat' : 'solid'}
              isLoading={busy}
              isDisabled={!current}
              onPress={() => (connected ? run(disconnect) : connectNow())}
            >
              {connected ? '断开连接' : '一键连接'}
            </Button>
            <div className="flex items-center justify-between gap-4 border-t border-default-200 pt-4">
              <div>
                <p className="font-semibold">虚拟网卡</p>
                <p className="text-tiny text-default-500">打开后电脑上所有软件都走代理</p>
              </div>
              <Switch
                isSelected={tunOn}
                isDisabled={!current || busy}
                onValueChange={(on) => run(on ? turnOnTun : turnOffTun)}
              />
            </div>
          </CardBody>
        </Card>

        <Card className="w-full max-w-md mt-4 shrink-0">
          <CardBody className="flex flex-col gap-3 p-5">
            <p className="font-semibold">{current ? '更换线路' : '导入线路'}</p>
            <Input
              size="md"
              placeholder="粘贴别人发给你的链接"
              value={url}
              onValueChange={setUrl}
              onKeyDown={(e) => {
                if (e.key === 'Enter') importLink()
              }}
            />
            <Button color="primary" isLoading={busy} isDisabled={!url.trim()} onPress={importLink}>
              导入
            </Button>
            <Button size="sm" variant="light" isDisabled={busy} onPress={importFile}>
              或者选择 yaml 文件导入
            </Button>
          </CardBody>
        </Card>
      </div>
    </div>
  )
}

export default CcHome
