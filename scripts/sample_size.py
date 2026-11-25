"""Approximate fixed-sample two-proportion design; not a substitute for pilot analysis."""
import argparse
from math import ceil,sqrt
from statistics import NormalDist


def estimate(p0, delta, alpha=.05, power=.8):
    p1=p0+delta
    if not 0<p0<p1<1: raise ValueError('Require 0 < baseline < baseline + delta < 1')
    pooled=(p0+p1)/2
    z=NormalDist()
    return ceil((z.inv_cdf(1-alpha/2)*sqrt(2*pooled*(1-pooled))+z.inv_cdf(power)*sqrt(p0*(1-p0)+p1*(1-p1)))**2/delta**2)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--baseline',type=float,required=True);p.add_argument('--delta',type=float,default=.05)
    args=p.parse_args();n=estimate(args.baseline,args.delta)
    print(f'Approximate {n} per arm, {2*n} total for detecting a nonzero effect; add allocation margin.\nThis is NOT powered to establish that the lower confidence bound exceeds the same delta. Use a larger assumed true effect or simulation for that stricter release gate.')
