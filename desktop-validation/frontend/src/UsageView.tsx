import type {Run} from './bridge'
import {RawRecord} from './records'

const value=(v:unknown)=>typeof v==='number'?v.toLocaleString('en-US'):'未知'
export function UsageView({run,expanded=false}:{run:Run;expanded?:boolean}){
 const report=run.usageReport
 if(!report)return <details className="usage-view"><summary>用量与预算 · 原始数据未知</summary><p>此记录没有可核实的 CLI 用量来源。</p></details>
 const roles:Record<string,string>={developer:'开发 / 修复',reviewer:'审查',acceptor:'验收'}
 return <details className="usage-view" data-usage-view open={expanded?true:undefined}><summary>用量与预算 <span>{value(report.cli_launches)} 次 CLI 启动 · 费用 UNKNOWN</span></summary>
 <p>预算上限 {value(report.cli_launch_limit)} 次 CLI 启动。CLI 内部请求次数未知；此限制不等于真实费用硬上限。</p>
 <div className="usage-totals">{(['input_tokens','output_tokens','cached_input_tokens'] as const).map((key,index)=><div key={key}><span>{['输入 Token','输出 Token','缓存读取（输入内）'][index]}</span><strong>{value(report.totals[key]?.known)}</strong>{report.totals[key]?.complete===false&&<small>部分已知</small>}</div>)}</div>
 <div className="usage-table"><table><thead><tr><th>Agent / 职责</th><th>输入</th><th>输出</th><th>缓存读取</th></tr></thead><tbody>{report.rounds.map(row=><tr key={row.round}><td>{row.executor} · {roles[row.role]||row.role}{row.simulation?' · Fake/Mock':''}</td><td>{value(row.input_tokens)}</td><td>{value(row.output_tokens)}</td><td>{value(row.cached_input_tokens)}</td></tr>)}</tbody></table></div>
 <p>输入统一为含缓存总量。Codex 缓存已包含在 input_tokens；固定版 Cursor 的 inputTokens 排除缓存，已按原始字段归一。真实模型名称和实际账单缺失时显示未知。</p>
 <RawRecord title="用量来源与逐轮快照" value={report}/>
 </details>
}
