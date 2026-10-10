#!/usr/bin/env python3
"""
基因型相关性分析 (Genotype Correlation Analysis)

功能:
  1. 质控 (QC): 缺失率、次等位基因频率 (MAF)、哈迪-温伯格平衡 (HWE) 检验
  2. SNP 间连锁不平衡 (LD): 基因型相关系数 r 与 r² 矩阵 + 热图
  3. 基因型-表型关联: 加性模型线性回归 (可加协变量) + Pearson/Spearman 相关
  4. 多重检验校正: Bonferroni 与 Benjamini-Hochberg FDR
  5. 输出结果表格与图 (LD 热图、-log10(P) 曼哈顿图、QQ 图)

输入格式:
  基因型文件 (CSV/TSV): 行为样本, 第一列为样本 ID, 其余每列一个 SNP,
                         编码为 0/1/2 (次等位基因个数), 缺失为 NA 或空。
                         也支持 PLINK --recodeA 生成的 .raw 文件。
  表型文件 (CSV/TSV, 可选): 第一列样本 ID, 包含表型列及协变量列。

用法示例:
  # 用模拟数据演示
  python genotype_correlation.py --demo

  # 真实数据
  python genotype_correlation.py -g geno.csv -p pheno.csv --trait BMI \
      --covar age sex --out results
"""

import argparse
import os

import numpy as np
import pandas as pd
from scipy import stats

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ----------------------------------------------------------------------------
# 数据读取
# ----------------------------------------------------------------------------
def read_table(path):
    sep = "," if path.endswith(".csv") else r"\s+"
    return pd.read_csv(path, sep=sep, engine="python")


def load_genotypes(path):
    """读取基因型矩阵, 返回 DataFrame (index=样本 ID, columns=SNP)。"""
    df = read_table(path)
    if path.endswith(".raw"):
        # PLINK .raw: FID IID PAT MAT SEX PHENOTYPE SNP1_A SNP2_C ...
        df = df.set_index("IID").drop(columns=["FID", "PAT", "MAT", "SEX", "PHENOTYPE"])
    else:
        df = df.set_index(df.columns[0])
    df.index = df.index.astype(str)
    return df.apply(pd.to_numeric, errors="coerce")


def load_phenotypes(path):
    df = read_table(path)
    df = df.set_index(df.columns[0])
    df.index = df.index.astype(str)
    return df


# ----------------------------------------------------------------------------
# 质控
# ----------------------------------------------------------------------------
def hwe_exact_p(n_aa, n_ab, n_bb):
    """Wigginton et al. (2005) HWE 精确检验。"""
    n_aa, n_ab, n_bb = int(n_aa), int(n_ab), int(n_bb)
    n = n_aa + n_ab + n_bb
    if n == 0:
        return np.nan
    n_rare = min(2 * n_aa + n_ab, 2 * n_bb + n_ab)
    n_common = 2 * n - n_rare
    probs = np.zeros(n_rare + 1)

    # 从期望杂合子数附近开始递推
    mid = int(n_rare * n_common / (2 * n))
    if (n_rare - mid) % 2:
        mid += 1
    probs[mid] = 1.0

    curr_homr, curr_homc = (n_rare - mid) // 2, (n_common - mid) // 2
    for het in range(mid, 1, -2):
        probs[het - 2] = probs[het] * het * (het - 1) / (4.0 * (curr_homr + 1) * (curr_homc + 1))
        curr_homr += 1
        curr_homc += 1

    curr_homr, curr_homc = (n_rare - mid) // 2, (n_common - mid) // 2
    for het in range(mid, n_rare - 1, 2):
        probs[het + 2] = probs[het] * 4.0 * curr_homr * curr_homc / ((het + 2.0) * (het + 1.0))
        curr_homr -= 1
        curr_homc -= 1

    probs /= probs.sum()
    p = probs[probs <= probs[n_ab] + 1e-12].sum()
    return min(1.0, p)


def snp_qc(geno, max_missing=0.1, min_maf=0.01, min_hwe_p=1e-6):
    """计算每个 SNP 的 QC 指标并给出是否通过。"""
    rows = []
    for snp in geno.columns:
        g = geno[snp].dropna()
        g = g[g.isin([0, 1, 2])]
        n0, n1, n2 = (g == 0).sum(), (g == 1).sum(), (g == 2).sum()
        n = n0 + n1 + n2
        miss = 1 - n / len(geno)
        af = (n1 + 2 * n2) / (2 * n) if n else np.nan
        maf = min(af, 1 - af) if n else np.nan
        rows.append(
            dict(snp=snp, n=n, n_0=n0, n_1=n1, n_2=n2,
                 missing_rate=miss, maf=maf, hwe_p=hwe_exact_p(n0, n1, n2))
        )
    qc = pd.DataFrame(rows).set_index("snp")
    qc["pass_qc"] = (
        (qc["missing_rate"] <= max_missing)
        & (qc["maf"] >= min_maf)
        & (qc["hwe_p"] >= min_hwe_p)
    )
    return qc


# ----------------------------------------------------------------------------
# SNP 间相关 (LD)
# ----------------------------------------------------------------------------
def ld_matrix(geno):
    """基于基因型剂量的成对 Pearson 相关 (成对删除缺失), 返回 r 矩阵。"""
    return geno.corr(method="pearson", min_periods=10)


def ld_pairs(r, min_r2=0.0):
    """把 r 矩阵展开成 SNP 对列表。"""
    snps = r.columns
    iu = np.triu_indices(len(snps), k=1)
    out = pd.DataFrame({
        "snp1": snps[iu[0]],
        "snp2": snps[iu[1]],
        "r": r.values[iu],
    })
    out["r2"] = out["r"] ** 2
    return out[out["r2"] >= min_r2].sort_values("r2", ascending=False).reset_index(drop=True)


# ----------------------------------------------------------------------------
# 基因型-表型关联
# ----------------------------------------------------------------------------
def linear_assoc(x, y, covar=None):
    """
    加性模型: y = b0 + b1*SNP + covariates + e
    返回 beta, se, t, p, n。
    """
    cols = [x] if covar is None else [x, covar]
    data = pd.concat(cols + [y], axis=1).dropna()
    n = len(data)
    if n < 10 or data.iloc[:, 0].nunique() < 2:
        return np.nan, np.nan, np.nan, np.nan, n

    X = np.column_stack([np.ones(n), data.iloc[:, :-1].to_numpy(float)])
    yv = data.iloc[:, -1].to_numpy(float)
    beta, _, rank, _ = np.linalg.lstsq(X, yv, rcond=None)
    df = n - rank
    if df <= 0:
        return np.nan, np.nan, np.nan, np.nan, n
    resid = yv - X @ beta
    sigma2 = resid @ resid / df
    cov = sigma2 * np.linalg.pinv(X.T @ X)
    se = np.sqrt(cov[1, 1])
    t = beta[1] / se
    p = 2 * stats.t.sf(abs(t), df)
    return beta[1], se, t, p, n


def bh_fdr(p):
    """Benjamini-Hochberg 校正, 保留 NaN。"""
    p = np.asarray(p, float)
    q = np.full_like(p, np.nan)
    ok = ~np.isnan(p)
    pv = p[ok]
    m = len(pv)
    if m == 0:
        return q
    order = np.argsort(pv)
    ranked = pv[order] * m / np.arange(1, m + 1)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    res = np.empty(m)
    res[order] = np.minimum(ranked, 1)
    q[ok] = res
    return q


def association(geno, trait, covar=None):
    rows = []
    for snp in geno.columns:
        x = geno[snp]
        beta, se, t, p, n = linear_assoc(x, trait, covar)
        pair = pd.concat([x, trait], axis=1).dropna()
        if len(pair) >= 3 and pair.iloc[:, 0].nunique() > 1:
            pr, pp = stats.pearsonr(pair.iloc[:, 0], pair.iloc[:, 1])
            sr, sp = stats.spearmanr(pair.iloc[:, 0], pair.iloc[:, 1])
        else:
            pr = pp = sr = sp = np.nan
        rows.append(dict(snp=snp, n=n, beta=beta, se=se, t=t, p=p,
                         pearson_r=pr, pearson_p=pp,
                         spearman_rho=sr, spearman_p=sp))
    res = pd.DataFrame(rows).set_index("snp")
    m = res["p"].notna().sum()
    res["p_bonferroni"] = np.minimum(res["p"] * m, 1)
    res["q_fdr"] = bh_fdr(res["p"])
    return res.sort_values("p")


# ----------------------------------------------------------------------------
# 作图
# ----------------------------------------------------------------------------
def plot_ld_heatmap(r, path, max_snps=100):
    r2 = r.iloc[:max_snps, :max_snps] ** 2
    fig, ax = plt.subplots(figsize=(8, 7))
    im = ax.imshow(r2, cmap="Reds", vmin=0, vmax=1)
    if len(r2) <= 40:
        ax.set_xticks(range(len(r2)))
        ax.set_yticks(range(len(r2)))
        ax.set_xticklabels(r2.columns, rotation=90, fontsize=7)
        ax.set_yticklabels(r2.index, fontsize=7)
    ax.set_title("Pairwise LD (r²)")
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04, label="r²")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_manhattan(res, path, snp_order, alpha=0.05):
    # 按原始 SNP 顺序绘制
    res = res.reindex(snp_order).dropna(subset=["p"])
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.scatter(range(len(res)), -np.log10(res["p"]), s=12, c="#3b6ea5")
    ax.axhline(-np.log10(alpha / len(res)), color="#c0392b", ls="--", lw=1,
               label=f"Bonferroni (α={alpha})")
    ax.set_xlabel("SNP")
    ax.set_ylabel("-log10(P)")
    ax.set_title("Genotype–phenotype association")
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_qq(res, path):
    p = np.sort(res["p"].dropna().to_numpy())
    exp = -np.log10(np.arange(1, len(p) + 1) / (len(p) + 1))
    obs = -np.log10(p)
    chi2 = stats.chi2.isf(p, 1)
    lam = np.median(chi2) / stats.chi2.ppf(0.5, 1)
    fig, ax = plt.subplots(figsize=(5, 5))
    ax.scatter(exp, obs, s=10, c="#3b6ea5")
    lim = max(exp.max(), obs.max()) * 1.05
    ax.plot([0, lim], [0, lim], color="grey", lw=1)
    ax.set_xlabel("Expected -log10(P)")
    ax.set_ylabel("Observed -log10(P)")
    ax.set_title(f"QQ plot (λ = {lam:.3f})")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return lam


# ----------------------------------------------------------------------------
# 模拟数据
# ----------------------------------------------------------------------------
def simulate(n_samples=500, n_snps=50, seed=42):
    """生成带 LD 区块和 3 个因果 SNP 的模拟数据。"""
    rng = np.random.default_rng(seed)
    block = 5
    geno = np.zeros((n_samples, n_snps), int)
    for b in range(0, n_snps, block):
        # 每个区块共享一个潜在单倍型变量, 产生块内 LD
        maf = rng.uniform(0.1, 0.5, size=block)
        latent = rng.normal(size=(n_samples, 2))
        for j in range(min(block, n_snps - b)):
            thr = stats.norm.ppf(1 - maf[j])
            hap = (0.8 * latent + 0.6 * rng.normal(size=(n_samples, 2))) > thr
            geno[:, b + j] = hap.sum(axis=1)

    ids = [f"S{i:04d}" for i in range(n_samples)]
    snps = [f"rs{1000 + j}" for j in range(n_snps)]
    geno_df = pd.DataFrame(geno, index=ids, columns=snps).astype(float)
    # 随机缺失 1%
    mask = rng.random(geno_df.shape) < 0.01
    geno_df = geno_df.mask(mask)

    age = rng.normal(45, 10, n_samples)
    sex = rng.integers(0, 2, n_samples)
    causal = {"rs1002": 0.5, "rs1017": -0.4, "rs1033": 0.3}
    y = 0.03 * age + 0.5 * sex + rng.normal(size=n_samples)
    for s, b in causal.items():
        y += b * np.nan_to_num(geno_df[s].to_numpy(), nan=geno_df[s].mean())
    pheno = pd.DataFrame({"trait": y, "age": age, "sex": sex}, index=ids)
    return geno_df, pheno


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="基因型相关性分析")
    ap.add_argument("-g", "--geno", help="基因型文件 (CSV/TSV/PLINK .raw)")
    ap.add_argument("-p", "--pheno", help="表型/协变量文件")
    ap.add_argument("--trait", help="表型列名")
    ap.add_argument("--covar", nargs="*", default=[], help="协变量列名")
    ap.add_argument("--out", default="genotype_results", help="输出目录")
    ap.add_argument("--max-missing", type=float, default=0.1)
    ap.add_argument("--min-maf", type=float, default=0.01)
    ap.add_argument("--min-hwe-p", type=float, default=1e-6)
    ap.add_argument("--min-r2", type=float, default=0.2, help="输出 LD 对的 r² 阈值")
    ap.add_argument("--demo", action="store_true", help="使用模拟数据运行")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    if args.demo:
        geno, pheno = simulate()
        args.trait = args.trait or "trait"
        args.covar = args.covar or ["age", "sex"]
        geno.to_csv(os.path.join(args.out, "demo_genotypes.csv"))
        pheno.to_csv(os.path.join(args.out, "demo_phenotypes.csv"))
    else:
        if not args.geno:
            ap.error("需要 --geno, 或使用 --demo")
        geno = load_genotypes(args.geno)
        pheno = load_phenotypes(args.pheno) if args.pheno else None

    print(f"读取: {geno.shape[0]} 个样本, {geno.shape[1]} 个 SNP")

    # 1. QC
    qc = snp_qc(geno, args.max_missing, args.min_maf, args.min_hwe_p)
    qc.to_csv(os.path.join(args.out, "snp_qc.csv"))
    geno_qc = geno.loc[:, qc.index[qc["pass_qc"]]]
    print(f"QC 后保留 {geno_qc.shape[1]} / {geno.shape[1]} 个 SNP")

    # 2. LD
    r = ld_matrix(geno_qc)
    r.to_csv(os.path.join(args.out, "ld_r_matrix.csv"))
    pairs = ld_pairs(r, args.min_r2)
    pairs.to_csv(os.path.join(args.out, "ld_pairs.csv"), index=False)
    plot_ld_heatmap(r, os.path.join(args.out, "ld_heatmap.png"))
    print(f"r² ≥ {args.min_r2} 的 SNP 对: {len(pairs)}")

    # 3. 关联分析
    if pheno is not None and args.trait:
        common = geno_qc.index.intersection(pheno.index)
        if len(common) == 0:
            raise SystemExit("基因型与表型文件没有共同的样本 ID")
        g = geno_qc.loc[common]
        y = pd.to_numeric(pheno.loc[common, args.trait], errors="coerce")
        covar = pheno.loc[common, args.covar].apply(pd.to_numeric, errors="coerce") \
            if args.covar else None
        res = association(g, y, covar)
        res.to_csv(os.path.join(args.out, "association_results.csv"))
        plot_manhattan(res, os.path.join(args.out, "manhattan.png"), g.columns)
        lam = plot_qq(res, os.path.join(args.out, "qq_plot.png"))

        sig = res[res["p_bonferroni"] < 0.05]
        print(f"关联分析样本数: {len(common)}, 基因组膨胀因子 λ = {lam:.3f}")
        print(f"Bonferroni 显著 SNP: {len(sig)}")
        cols = ["beta", "se", "p", "p_bonferroni", "q_fdr", "pearson_r"]
        print(res[cols].head(10).to_string(float_format=lambda v: f"{v:.3g}"))

    print(f"\n结果已保存至: {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
