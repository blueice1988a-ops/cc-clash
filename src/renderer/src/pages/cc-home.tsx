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
  getFilePath,
  getProfileConfig,
  grantTunPermissions,
  readTextFile,
  removeProfileItem,
  restartAsAdmin,
  setOperationMode,
  showTunPermissionDialog,
  triggerSysProxy,
  updateTrayIconImmediate
} from '@renderer/utils/ipc'
import { markInitialContentPartReady } from '@renderer/utils/startup'
import ccIcon from '@renderer/assets/cc-icon.png'

function refreshTray(): void {
  window.electron.ipcRenderer.send('updateFloatingWindow')
  window.electron.ipcRenderer.send('updateTrayMenu')
}

// Cc clash 的唯一界面：导入一条链接，然后自动打开系统代理和虚拟网卡
const CcHome: React.FC = () => {
  const { appConfig, patchAppConfig } = useAppConfig()
  const { controledMihomoConfig, patchControledMihomoConfig } = useControledMihomoConfig()
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
    setOperationMode('standard').catch((e) => toast.error(String(e)))
  }, [appConfig])

  const enableTun = async (): Promise<boolean> => {
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
    return true
  }

  const connect = async (): Promise<void> => {
    if (controledMihomoConfig?.mode !== 'rule') await patchControledMihomoConfig({ mode: 'rule' })
    await patchAppConfig({ sysProxy: { enable: true } })
    await triggerSysProxy(true)
    const tun = await enableTun()
    updateTrayIconImmediate(true, tun)
    refreshTray()
  }

  const disconnect = async (): Promise<void> => {
    await patchAppConfig({ sysProxy: { enable: false } })
    await triggerSysProxy(false)
    if (tunOn) await patchControledMihomoConfig({ tun: { enable: false } })
    updateTrayIconImmediate(false, false)
    refreshTray()
  }

  const run = async (action: () => Promise<void>): Promise<void> => {
    if (busy) return
    setBusy(true)
    try {
      await action()
    } catch (e) {
      toast.error(String(e))
    } finally {
      setBusy(false)
      mutateProfileConfig()
    }
  }

  // 只保留刚导入的这一条线路，然后连上
  const importProfile = async (item: Partial<IProfileItem>): Promise<void> => {
    const before = new Set((await getProfileConfig(true)).items.map((i) => i.id))
    await addProfileItem(item)
    const config = await getProfileConfig(true)
    const added =
      config.items.find((i) => !before.has(i.id)) ??
      config.items.find((i) => item.url && i.url === item.url)
    if (!added) throw new Error('导入失败，请检查链接是否正确')
    if (config.current !== added.id) await changeCurrentProfile(added.id)
    for (const old of config.items) {
      if (old.id !== added.id) await removeProfileItem(old.id)
    }
    await connect()
    try {
      if (!(await checkAutoRun())) await enableAutoRun()
    } catch {
      // 开机自启失败不影响使用
    }
    setUrl('')
    toast.success('导入成功，已经连上了')
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
        <img src={ccIcon} alt="" className="h-20 w-20 mt-4" />
        <h1 className="text-2xl font-bold mt-3">Cc clash</h1>

        <Card className="w-full max-w-md mt-8">
          <CardBody className="flex flex-row items-center justify-between gap-4 p-5">
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
              {connected && !tunOn && (
                <p className="text-tiny text-warning mt-1">
                  虚拟网卡没有打开，部分软件可能不走代理
                </p>
              )}
            </div>
            <Switch
              size="lg"
              isSelected={connected}
              isDisabled={!current || busy}
              onValueChange={(on) => run(on ? connect : disconnect)}
            />
          </CardBody>
        </Card>

        <Card className="w-full max-w-md mt-4">
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
