# 材料谱系与检验追溯

物料事件按来源偏移入账，拆分、转化和发货保持数量守恒。检验样品与放行处置有独立修订，谱系可追溯至原始来料。

运行需要 Python 3.11 或更高版本，使用标准库与 SQLite，无需第三方包。演练中的机构、人员、交易及读数全部为本地合成数据。

## 构建与测试

```sh
python3 -m compileall -q chainledger
python3 -m unittest discover -s tests -v
```

## 本地运行

```sh
python3 -m chainledger demo
python3 -m chainledger study
python3 -m chainledger --help
```

演练使用临时数据库，不会连接远程服务。命令以 JSON 显示业务结果；测试覆盖核心状态、边界输入和错误路径。运行前可用 `python3 --version` 核对解释器版本。

## 基于抽检证据的供应质量分析

`quality_study` 在放行流程之外提供只读分析，为下一月复检安排提供依据；它从不自动冻结物料，原放行规则保持不变。

- **可比层**：按检验规格及其内容摘要（规格不可变，修订即新层）、物料和时间范围分层。单位是有明确抽样方式的独立实物样品；称重数量、重复测量、同一样品的复检和撤回判定都不增加独立样本数。便利取样与代表性随机抽样分开列出，便利样品绝不推断整批缺陷率。
- **模型**：层内供应商间 Beta–Binomial 经验贝叶斯。保留每批缺陷数与检验数，批缺陷率服从所属供应商的 Beta 分布（批内二项、批间离散 κ 分开），供应商均值收缩到共同 Beta 先验。共同超参数由边际似然拟合（矩估计兜底），数值积分给出各供应商均值的后验区间和超过预设不可接受比例的概率。先验估计报告边界与失败（单供应商、零缺陷、极小样本、全部同率都有明确退路），绝不把所有样品合成一个二项分布。可选用外部预登记先验，输出中与本期数据估计的先验明确区分。
- **验证**：按时间滚动、整批留出，每个截止点只用当时可知的批次与判定拟合，对后续批次比较预测区间覆盖率与对数预测分数，并与不收缩的供应商独立估计对照；未来规格修订与后来的复检不会泄漏进训练。
- **建议**：列出高风险（超越概率高）与高不确定（区间宽）两类复检名单，同时显示原始样本、收缩前后的估计差别；供应商规模只作为证据权重，绝不直接充当风险分。
- **研究**：研究冻结样品、规格和判定版本并带摘要；后续更正（复检、撤回）通过 `quality_study_compare` 说明排名改变的证据。研究可导出为带摘要的 JSON 并离线核验。

```sh
python3 -m chainledger --db chain.sqlite3 run study-action.json
```

动作示例：`inspection_sample`（可带 `sampling: "random"`）、`inspection_recheck`、`inspection_withdraw`、`quality_study`、`quality_study_get`、`quality_study_list`、`quality_study_compare`、`quality_study_export`、`quality_study_verify`。`python3 -m chainledger study` 用两个小样本极端供应商和一个大样本稳定供应商走通研究、比较与导出。
