import type {RefObject} from 'react'
import {X} from 'lucide-react'
import type {Project, ProjectDetails} from './bridge'
import {AccessPanel} from './AccessPanel'
import {gitBranch} from './composeModel'
import {FloatingPanel} from './FloatingPanel'

export function ProjectInfoPanel({open, anchorRef, onClose, project, overview, enabled, onOpenManager, onRehearse}: {
  open: boolean
  anchorRef: RefObject<HTMLButtonElement | null>
  onClose: () => void
  project: Project | undefined
  overview: ProjectDetails | null
  enabled: boolean
  onOpenManager: () => void
  onRehearse: () => void
}) {
  const git = overview?.git
  const branch = gitBranch(git?.branch)
  return <FloatingPanel open={open} anchorRef={anchorRef} label="项目信息" onClose={onClose}>
    <header className="floating-head">
      <h2>项目信息</h2>
      <button type="button" data-autofocus aria-label="关闭项目信息" onClick={onClose}><X size={16}/></button>
    </header>
    <div className="floating-body">
      <p className="microcopy">工作区、Git 与执行权限。登记本身不授予执行权限。</p>
      <p className="preserve-text project-path">{project?.workspace || '未读取工作区路径'}</p>
      <p className="microcopy">Git{branch ? ` · ${branch}` : ''} · {git?.summary || '正在读取'}</p>
      {git?.error && <p className="microcopy">{git.error}</p>}
      {project?.configError && <p className="microcopy" role="alert">{project.configError}</p>}
      <AccessPanel projectId={project?.id || ''} enabled={enabled && !!project?.registered} embedded/>
      <div className="panel-actions">
        {project?.registered && enabled && <button type="button" className="secondary-button" onClick={onOpenManager}>查看项目配置</button>}
        {enabled && <button type="button" className="secondary-button" data-create-rehearsal="info" onClick={onRehearse}>创建安全演练</button>}
      </div>
    </div>
  </FloatingPanel>
}
