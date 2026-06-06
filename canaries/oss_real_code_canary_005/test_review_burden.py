import sys
from review_burden import classify_review_burden

def test_classify_review_burden():
    assert classify_review_burden(0) == 'none'
    assert classify_review_burden(0.0) == 'none'
    assert classify_review_burden(0.25) == 'minor'
    assert classify_review_burden(0.6) == 'moderate'
    assert classify_review_burden(1.0) == 'major'
    assert classify_review_burden(0.1) == 'minor'
    assert classify_review_burden(0.26) == 'moderate'
    assert classify_review_burden(0.61) == 'major'
    try:
        classify_review_burden(-0.1)
        raise AssertionError("Expected ValueError")
    except ValueError:
        pass
    try:
        classify_review_burden(-1.0)
        raise AssertionError("Expected ValueError")
    except ValueError:
        pass
    print("All tests passed.")
    return 0

if __name__ == "__main__":
    sys.exit(test_classify_review_burden())
