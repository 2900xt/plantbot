"""Binary metrics with explicit abstentions; never count unknown as healthy."""
def metrics(rows):
    counts={key:0 for key in ['tp','tn','fp','fn','unknown_infected','unknown_healthy']}
    for row in rows:
        expected,predicted=row['expected_infected'],row['predicted_infected']
        if predicted is None:key='unknown_infected' if expected else 'unknown_healthy'
        elif predicted:key='tp' if expected else 'fp'
        else:key='fn' if expected else 'tn'
        counts[key]+=1
    tp,tn,fp,fn=(counts[k] for k in ['tp','tn','fp','fn'])
    ratio=lambda a,b:a/b if b else None
    classified=tp+tn+fp+fn;total=len(rows)
    sensitivity=ratio(tp,tp+fn);specificity=ratio(tn,tn+fp)
    return {**counts,'total':total,'classified':classified,'unknown':total-classified,
        'accuracy_on_classified':ratio(tp+tn,classified),'correct_decisions_per_submitted':ratio(tp+tn,total),
        'coverage':ratio(classified,total),'precision':ratio(tp,tp+fp),'sensitivity_on_classified':sensitivity,
        'specificity_on_classified':specificity,'f1_on_classified':ratio(2*tp,2*tp+fp+fn),
        'balanced_accuracy_on_classified':(sensitivity+specificity)/2 if sensitivity is not None and specificity is not None else None,
        'infected_detection_per_infected_submitted':ratio(tp,tp+fn+counts['unknown_infected'])}

if __name__=='__main__':
    rows=[{'expected_infected':e,'predicted_infected':p} for e,p in [(True,True),(False,False),(False,True),(True,False),(True,None),(False,None)]]
    m=metrics(rows)
    assert m['tp']==m['tn']==m['fp']==m['fn']==m['unknown_infected']==m['unknown_healthy']==1
    assert m['accuracy_on_classified']==.5 and m['correct_decisions_per_submitted']==1/3 and m['coverage']==2/3
    assert m['sensitivity_on_classified']==m['specificity_on_classified']==m['balanced_accuracy_on_classified']==.5
    assert metrics([])['accuracy_on_classified'] is None
    assert metrics([{'expected_infected':True,'predicted_infected':None}])['classified']==0
    print('Binary confusion matrix, rejection accounting and empty-set checks passed')
