// These are uncalibrated model scores. Thresholds need independent camera data.
export function probabilityVector(classes, probabilities) {
  if (!Array.isArray(classes) || classes.length !== 38 || new Set(classes).size !== 38 || !classes.every(label => typeof label === 'string' && label.length > 0 && label.length <= 200) || !classes.some(label => label.endsWith('_healthy')) || !classes.some(label => !label.endsWith('_healthy')) || !Array.isArray(probabilities) || probabilities.length !== classes.length || !probabilities.every(p => typeof p === 'number' && Number.isFinite(p) && p >= 0 && p <= 1) || Math.abs(probabilities.reduce((a,b) => a+b,0)-1) > .001) throw new Error('Expected complete normalized 38-class probabilities');
  return probabilities.reduce((sum,p,i) => sum + (classes[i].endsWith('_healthy') ? 0 : p),0);
}

export function applyBinaryTriage(c, result) {
  if (!c.binaryTriage || result.simulated) return result;
  const score = Math.min(1,Math.max(0,probabilityVector(result.classes,result.probabilities)));
  const verdict = score >= c.binaryDiseaseThreshold ? 'diseased' : 'healthy';
  return {...result, verdict, original_verdict: result.verdict,
    disease_score: score, healthy_score: 1-score, binary_score_type:'summed_class_softmax_uncalibrated',
    binary_policy:{disease_threshold:c.binaryDiseaseThreshold,provisional:true}};
}
